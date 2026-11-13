# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 — shipped agents run in the background and compact at 750k tokens.

Owner 2026-11-10: foreground subagents are a bad user experience with little
value — a VCO-installed project always runs its subagents in the BACKGROUND.
Claude Code agent definitions support ``background: true`` ("always run as a
background task"), and since 2.1.296 ``autoCompactWindow`` (a subagent
auto-compacts earlier than the main conversation; 2.1.312: the key only ever
LOWERS the window, never raises it). Every shipped definition under
``templates/agents/`` (``free/`` + ``module-gateway/``) and every opt-in pack
agent (``templates/packs/*/agents/``, owner: ALL subagents background)
therefore carries ``background: true`` and ``autoCompactWindow: 750000`` —
on a model whose window is larger (1M) a lane compacts at 750k tokens, so it
does not grow until its report quality degrades; on a smaller window the
model's own auto-compact applies (the key never raises a window).

Delivery keeps both keys: the gated set lands byte-identical
(``test_install_bundle.py::ModuleGatewayAgentDeliveryTests``) and the free
set goes through ``vco_lib.materialize``'s token substitution, which touches
``{{TOKEN}}`` scalars only — a frontmatter key carrying no token is copied
unchanged. The delivery legs below prove both on a real bundle run.

The set scanned here is exactly the two shipped directories — ``_archive/``
is not shipped and ``WORKTREE_ISOLATION_GUIDE.md`` is a doc (its frontmatter
carries no ``name:``), so neither is an agent definition.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tests.common.module_gateway import MODULE_GATEWAY_AGENT_FILES

REPO_ROOT = Path(__file__).resolve().parents[1]
FREE_DIR = REPO_ROOT / "templates" / "agents" / "free"
GATED_DIR = REPO_ROOT / "templates" / "agents" / "module-gateway"
PACKS_DIR = REPO_ROOT / "templates" / "packs"

#: The two frontmatter keys every shipped agent carries, with their values.
BACKGROUND_KEY = "background"
AUTOCOMPACT_KEY = "autoCompactWindow"
AUTOCOMPACT_VALUE = 750_000


def _pack_agent_files() -> list[Path]:
    return sorted(PACKS_DIR.glob("*/agents/*.md"))


def _shipped_agent_files() -> list[Path]:
    return (sorted(FREE_DIR.glob("*.md")) + sorted(GATED_DIR.glob("*.md"))
            + _pack_agent_files())


SHIPPED_AGENTS = _shipped_agent_files()


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path.name}: no frontmatter block"
    data = yaml.safe_load(text.split("---\n", 2)[1])
    assert isinstance(data, dict), f"{path.name}: frontmatter is not a mapping"
    return data


def test_the_scan_covers_the_whole_shipped_set() -> None:
    """A parametrized test over an empty glob passes vacuously — pin the
    catalogue the scan must find (11 free + 12 gated + every pack agent the
    packs table lists), exactly as ``test_v02100_shipped_frontmatter_parses``
    pins its own scan."""
    from vco_lib import packs

    assert len(list(FREE_DIR.glob("*.md"))) == 11
    assert len(list(GATED_DIR.glob("*.md"))) == len(MODULE_GATEWAY_AGENT_FILES) == 12
    listed = sorted(m for p in packs.load_packs(REPO_ROOT).values()
                    for m in p.members if "/agents/" in m)
    assert [f"{p.parent.parent.name}/agents/{p.name}" for p in _pack_agent_files()] == listed
    assert len(listed) == 19
    assert len(SHIPPED_AGENTS) == 23 + len(listed)


@pytest.mark.parametrize("path", SHIPPED_AGENTS, ids=lambda p: p.name)
def test_every_shipped_agent_runs_in_the_background(path: Path) -> None:
    fm = _frontmatter(path)
    assert fm.get(BACKGROUND_KEY) is True, (
        f"{path.name}: frontmatter lacks `background: true` — a VCO-shipped "
        "agent always runs as a background task"
    )


@pytest.mark.parametrize("path", SHIPPED_AGENTS, ids=lambda p: p.name)
def test_every_shipped_agent_compacts_at_750k(path: Path) -> None:
    fm = _frontmatter(path)
    value = fm.get(AUTOCOMPACT_KEY)
    assert isinstance(value, int) and not isinstance(value, bool), (
        f"{path.name}: `{AUTOCOMPACT_KEY}` must be an integer token count, "
        f"got {value!r}"
    )
    assert value == AUTOCOMPACT_VALUE, (
        f"{path.name}: `{AUTOCOMPACT_KEY}` is {value!r}, expected "
        f"{AUTOCOMPACT_VALUE} (a subagent compacts below the main "
        "conversation's window)"
    )


# ---------------------------------------------------------------------------
# Delivery legs — a real bundle run must land both keys on disk.
# ---------------------------------------------------------------------------


def _fake_orchestrator(root: Path) -> Path:
    """A minimal orchestrator tree whose ``templates/agents`` is the REAL
    shipped set (delivery fidelity is the thing under test)."""
    (root / "templates").mkdir(parents=True)
    (root / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for name in ("settings.json.linux.template", "settings.json.windows.template"):
        shutil.copy(REPO_ROOT / "templates" / name, root / "templates" / name)
    shutil.copytree(REPO_ROOT / "templates" / "agents",
                    root / "templates" / "agents")
    shutil.copytree(PACKS_DIR, root / "templates" / "packs")
    return root


def _bundle_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *,
                    gateway_on: bool, packs: frozenset = frozenset()) -> Path:
    """Run a real bundle install into a fresh project; return its folder."""
    from tests.common.launcher_db_fixture import insert_rows, make_launcher_db

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    orch = _fake_orchestrator(tmp_path / "orch")
    folder = tmp_path / "proj"
    (folder / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": folder}])
    if gateway_on:
        insert_rows(db, "project_modules", [{
            "project_id": "p1", "module_name": "model_gateway", "enabled": 1,
        }])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    from vco_lib import project_init

    project_init.install_project_bundle(folder, orchestrator_root=orch,
                                        update_mode=True, packs=set(packs))
    return folder


def _assert_delivered_keys(folder: Path, names: list[str]) -> None:
    for name in names:
        delivered = folder / ".claude" / "agents" / name
        assert delivered.exists(), f"{name} was not delivered"
        fm = _frontmatter(delivered)
        assert fm.get(BACKGROUND_KEY) is True, (
            f"delivered {name} lost `background: true`")
        assert fm.get(AUTOCOMPACT_KEY) == AUTOCOMPACT_VALUE, (
            f"delivered {name} lost `{AUTOCOMPACT_KEY}: {AUTOCOMPACT_VALUE}`")


def test_delivery_keeps_the_keys_for_the_free_set(tmp_path, monkeypatch) -> None:
    folder = _bundle_install(tmp_path, monkeypatch, gateway_on=False)
    _assert_delivered_keys(folder, sorted(p.name for p in FREE_DIR.glob("*.md")))


def test_delivery_keeps_the_keys_for_the_gateway_set(tmp_path, monkeypatch) -> None:
    """The module-gated bucket delivers byte-identical copies with the module
    row on (the suite's conftest answers "every vendor key resolves")."""
    folder = _bundle_install(tmp_path, monkeypatch, gateway_on=True)
    _assert_delivered_keys(folder, list(MODULE_GATEWAY_AGENT_FILES))


def test_delivery_keeps_the_keys_for_every_pack_agent(tmp_path, monkeypatch) -> None:
    """Q1 (owner: ALL subagents background): a bundle install with every pack
    selected lands each pack agent with both keys."""
    from vco_lib import packs as _packs

    table = _packs.load_packs(REPO_ROOT)
    folder = _bundle_install(tmp_path, monkeypatch, gateway_on=False,
                             packs=frozenset(table))
    _assert_delivered_keys(folder, [p.name for p in _pack_agent_files()])


# ---------------------------------------------------------------------------
# Guidance pins — the shipped coordinators' instructions must say what the
# frontmatter does, or the two drift apart.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["CLAUDE.md.template", "ORCHESTRATOR-CLAUDE.md.template"])
def test_shipped_guidance_says_agents_run_in_the_background(name: str) -> None:
    text = (REPO_ROOT / "templates" / name).read_text(encoding="utf-8")
    assert "background: true" in text, name
    assert "autoCompactWindow: 750000" in text, name
    # Review C-M5: the 750k bound is stated only for a larger (1M) window,
    # and the helper rule stays for a helper started with run_in_background.
    assert "on a smaller window the model's own auto-compact applies" in text, name
    assert "runs its own helpers in the foreground" in text, name
