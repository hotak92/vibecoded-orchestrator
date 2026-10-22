# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.104 — the hub comes back after a reboot from ANY VCO project.

Owner report 2026-10-22: after a reboot no VS Code session restarted vct-hub;
it came back only when the launcher was opened, so the model gateway could not
read keychain vendor keys ("503 no key found for vendor 'qwen'"). Two causes:

1. ``session-start-ensure-hub`` passes ``--repo-root <hook dir>/../..`` — the
   PROJECT root — and :func:`vco_lib.hub_ensure.find_dist_binary` let that
   argument REPLACE the orchestrator checkout. Only the orchestrator root has
   a ``launcher/dist/``, so from every other project the hub (and the
   launcher, through the same chain) was "binary_not_found".
2. The hook was registered under SessionStart matcher ``startup`` only.
   VS Code restores existing chats after a reboot, and a restored chat fires
   SessionStart with source ``resume`` — so no hook ran at all.

Contract pinned here:

* step 2 of the discovery chain probes the caller's root FIRST, then the
  orchestrator clone (the ONE install-root resolver, then the checkout);
* both settings templates register ``session-start-ensure-hub`` under a
  second SessionStart group ``resume|clear|compact`` (async, same timeout),
  so every session source ensures the hub exactly once; the context/KG hooks
  are NOT widened;
* the bundle-update settings merge delivers that group to a project carrying
  the old ``startup``-only shape, once, idempotently — and never to a project
  whose user disabled the hook in the launcher;
* end to end: a RESUMED session in a NON-root project whose hub is down
  starts the hub from the orchestrator's dist; a running hub is left alone.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vco_lib import hub_ensure, launcher_ensure, project_init, settings_merge
from vco_lib.parked_hooks import ParkedHook, ParkedHooksState

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
LINUX = REPO / "templates" / "settings.json.linux.template"
WINDOWS = REPO / "templates" / "settings.json.windows.template"
TEMPLATES = {"linux": LINUX, "windows": WINDOWS}
EXT = {"linux": "sh", "windows": "ps1"}
RESUME_MATCHER = "resume|clear|compact"
SERVICE_HOOK = "session-start-ensure-hub"

needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="bash .sh leg; Windows runs the .ps1 sibling: "
           "tests/test_v02104_hub_resume_ensure.py::test_the_ps1_sibling_reports_through_the_same_envelope")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No override, no PATH copy, no ~/.vct/bin copy — only step 2 can answer."""
    for var in ("VCT_HUB_BIN", launcher_ensure.LAUNCHER_BIN_ENV,
                "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))


def _fake_orchestrator(root: Path, stems=("vct-hub",), body: str = "#!/bin/sh\nexit 0\n") -> dict:
    """A directory that passes ``looks_like_orchestrator_root`` and carries
    executables in its arch-qualified ``launcher/dist`` slot."""
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    arch = hub_ensure.dist_arch_dir()
    slot = root / "launcher" / "dist" / arch if arch else root / "launcher" / "dist"
    slot.mkdir(parents=True, exist_ok=True)
    out = {}
    for stem in stems:
        exe = slot / stem
        exe.write_text(body, encoding="utf-8")
        exe.chmod(0o755)
        out[stem] = exe
    return out


def _checkout_is(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make ``root`` the checkout vco_lib "runs from" for BOTH fallback rungs
    (the install-root resolver's last rung and ``_default_repo_root``)."""
    from vco_lib import python_exe

    real = python_exe.resolve_install_root

    def fake(explicit=None, *, env=None):  # type: ignore[no-untyped-def]
        found = real(explicit, env=env)
        here = Path(python_exe.__file__).resolve().parent.parent
        return root if found is None or found == here else found

    monkeypatch.setattr(python_exe, "resolve_install_root", fake)
    monkeypatch.setattr(hub_ensure, "_default_repo_root", lambda: root)


def _project(root: Path) -> Path:
    (root / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# 1. discovery: a project root no longer hides the orchestrator's dist
# ---------------------------------------------------------------------------


def test_a_project_root_finds_the_orchestrator_hub_via_the_install_root_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The reported bug. RED before the fix: ``repo_root`` replaced the
    checkout, so the project root's missing dist was the whole answer."""
    _isolate(monkeypatch, tmp_path)
    orch = _fake_orchestrator(tmp_path / "orch")
    monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(tmp_path / "orch"))
    project = _project(tmp_path / "SomeProject")

    assert hub_ensure.find_hub_binary(repo_root=project) == orch["vct-hub"]


def test_a_project_root_finds_the_hub_of_the_checkout_vco_lib_runs_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """With no install-root env at all, the checkout this module is imported
    from is the fallback anchor (what the hooks run: the orchestrator venv
    imports vco_lib from the orchestrator checkout). RED before the fix."""
    _isolate(monkeypatch, tmp_path)
    orch = _fake_orchestrator(tmp_path / "orch")
    _checkout_is(monkeypatch, tmp_path / "orch")
    project = _project(tmp_path / "SomeProject")

    assert hub_ensure.find_hub_binary(repo_root=project) == orch["vct-hub"]
    res = hub_ensure.main(["resolve", "--json", "--repo-root", str(project)])
    assert res == 0


def test_the_launcher_leg_gets_the_same_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``launcher_ensure`` walks the same ONE chain (``find_dist_binary``),
    so the launcher is found from a project root too."""
    _isolate(monkeypatch, tmp_path)
    stem = launcher_ensure.launcher_binary_stem()
    orch = _fake_orchestrator(tmp_path / "orch", stems=(stem,))
    _checkout_is(monkeypatch, tmp_path / "orch")
    project = _project(tmp_path / "SomeProject")

    assert launcher_ensure.find_launcher_binary(repo_root=project, env={}) == orch[stem]


def test_the_hub_lookup_answers_the_environment_it_is_handed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """R29 NIT: ``find_hub_binary`` threads ``env`` like its siblings — the
    install-root rung reads the mapping passed in, not ``os.environ``."""
    _isolate(monkeypatch, tmp_path)
    orch = _fake_orchestrator(tmp_path / "orch")
    project = _project(tmp_path / "SomeProject")
    _checkout_is(monkeypatch, tmp_path / "no-checkout")
    handed = {"VCT_ORCHESTRATOR_ROOT": str(tmp_path / "orch"), "HOME": str(tmp_path / "no-home")}

    assert hub_ensure.find_hub_binary(repo_root=project, env=handed) == orch["vct-hub"]
    # Leave-alone: the real environment names no clone, so without the
    # mapping nothing is found.
    assert hub_ensure.find_hub_binary(repo_root=project) is None


def test_the_callers_own_root_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An explicit ``repo_root`` with its own dist copy keeps precedence over
    the orchestrator fallback (the orchestrator root itself, a dev clone)."""
    _isolate(monkeypatch, tmp_path)
    _fake_orchestrator(tmp_path / "orch")
    own = _fake_orchestrator(tmp_path / "dev-clone")
    monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(tmp_path / "orch"))

    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "dev-clone") == own["vct-hub"]


def test_the_launcher_process_still_never_probes_any_checkout_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``probe_repo_dist=False`` (the Rust launcher's ``--no-repo-dist``)
    switches off the WHOLE of step 2's dist probing, the new fallback
    included — a ``cargo run`` launcher must not gain a hub it never had."""
    _isolate(monkeypatch, tmp_path)
    _fake_orchestrator(tmp_path / "orch")
    monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(tmp_path / "orch"))
    monkeypatch.setattr(hub_ensure, "_default_repo_root", lambda: tmp_path / "orch")

    assert hub_ensure.find_hub_binary(
        repo_root=tmp_path / "SomeProject", probe_repo_dist=False) is None


def test_a_bogus_install_root_env_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The env rung only counts when it names a real clone (the one
    resolver's rule): a stray dist under a non-clone is not picked up."""
    _isolate(monkeypatch, tmp_path)
    slot = tmp_path / "not-a-clone" / "launcher" / "dist"
    slot.mkdir(parents=True)
    exe = slot / "vct-hub"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("VCT_ORCHESTRATOR_ROOT", str(tmp_path / "not-a-clone"))

    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "SomeProject") != exe


# ---------------------------------------------------------------------------
# 2. registration: the hub is ensured on every session source
# ---------------------------------------------------------------------------


def _session_start(doc: dict) -> list:
    return doc["hooks"]["SessionStart"]


def _matchers_for(doc: dict, stem: str) -> list:
    return [
        g.get("matcher") or ""
        for g in _session_start(doc)
        for h in g.get("hooks", [])
        if f"{stem}." in h.get("command", "")
    ]


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_the_hub_hook_runs_on_every_session_source_exactly_once(flavour: str) -> None:
    doc = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    matchers = _matchers_for(doc, SERVICE_HOOK)
    assert sorted(matchers) == sorted(["startup", RESUME_MATCHER]), matchers
    covered = [s for m in matchers for s in m.split("|")]
    for source in ("startup", "resume", "clear", "compact"):
        assert covered.count(source) == 1, (source, matchers)
    group = next(g for g in _session_start(doc) if g.get("matcher") == RESUME_MATCHER)
    assert len(group["hooks"]) == 1
    hook = group["hooks"][0]
    startup = next(
        h for g in _session_start(doc) if g.get("matcher") == "startup"
        for h in g["hooks"] if f"{SERVICE_HOOK}." in h["command"])
    assert hook == startup, "the resume registration must be the startup one, verbatim"
    assert hook.get("async") is True


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_the_context_hooks_are_not_widened(flavour: str) -> None:
    """Only the service ensure moves: re-injecting context on resume was
    withdrawn deliberately (W6-K), and the KG/deferral surfaces are
    startup reads."""
    doc = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    for stem in ("compact-context-reinject", "session-start-kg-loader",
                 "session-start-deferral-surface", "context-size-check"):
        assert not any("resume" in m for m in _matchers_for(doc, stem)), stem


# ---------------------------------------------------------------------------
# 3. delivery: the bundle update brings the new group to existing projects
# ---------------------------------------------------------------------------


def _old_shape(flavour: str) -> dict:
    """The v0.2.103 template: the same file minus the resume group."""
    doc = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    doc["hooks"]["SessionStart"] = [
        g for g in _session_start(doc) if g.get("matcher") != RESUME_MATCHER]
    return doc


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_bundle_update_delivers_the_resume_registration(flavour: str, tmp_path: Path) -> None:
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_old_shape(flavour), indent=2) + "\n", encoding="utf-8")

    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False, parked=ParkedHooksState(readable=True))
    assert status == "merged"
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert sorted(_matchers_for(merged, SERVICE_HOOK)) == sorted(["startup", RESUME_MATCHER])

    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False, parked=ParkedHooksState(readable=True))
    assert status == "unchanged", "the second update must change nothing"


def _parked_everywhere(flavour: str) -> tuple[dict, ParkedHooksState]:
    """The old shape with the hub hook DISABLED from the launcher Hooks tab:
    removed from settings.json, parked under SessionStart/startup."""
    doc = _old_shape(flavour)
    command = ""
    for g in _session_start(doc):
        kept = []
        for h in g["hooks"]:
            if f"{SERVICE_HOOK}." in h["command"]:
                command = h["command"]
            else:
                kept.append(h)
        g["hooks"] = kept
    assert command
    state = ParkedHooksState(readable=True, hooks=(
        ParkedHook(event="SessionStart", matcher="startup", command=command, is_async=True),))
    return doc, state


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_hook_the_user_disabled_is_not_switched_back_on_for_resume(flavour: str) -> None:
    """RED before the parked-match change: the template ships the hook under
    two matchers, so the startup-parked row did not name the new resume
    registration and the merge added it — half re-enabling a disabled hook."""
    user, parked = _parked_everywhere(flavour)
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    kept_out: list = []
    merged = settings_merge.smart_merge_settings(
        user, template, parked=parked, kept_out=kept_out)
    assert _matchers_for(merged, SERVICE_HOOK) == []
    assert {r["matcher"] for r in kept_out if SERVICE_HOOK in r["command"]} == {
        "startup", RESUME_MATCHER}


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_hook_disabled_on_one_matcher_only_still_gets_the_new_one(flavour: str) -> None:
    """The leave-alone side of that rule: a hook still running under startup
    was disabled for ONE matcher only (here: a stale compact row), so the
    new resume registration is added."""
    user = _old_shape(flavour)
    command = next(
        h["command"] for g in _session_start(user) for h in g["hooks"]
        if f"{SERVICE_HOOK}." in h["command"])
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook(event="SessionStart", matcher="compact", command=command),))
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    merged = settings_merge.smart_merge_settings(user, template, parked=parked, kept_out=[])
    assert sorted(_matchers_for(merged, SERVICE_HOOK)) == sorted(["startup", RESUME_MATCHER])


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_every_sibling_row_is_live_in_both_templates(flavour: str) -> None:
    """A MATCHER_SIBLINGS row is only true while the template ships each
    target under BOTH the added matcher and its sibling."""
    from vco_lib.hook_retirements import MATCHER_SIBLINGS

    doc = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    assert MATCHER_SIBLINGS
    for row in MATCHER_SIBLINGS:
        for target in row.targets:
            if not target.endswith("." + EXT[flavour]):
                continue
            stem = target.rsplit(".", 1)[0]
            matchers = {
                g.get("matcher") or ""
                for g in doc["hooks"][row.event]
                for h in g.get("hooks", [])
                if f"/{target}" in h.get("command", "")
            }
            assert {row.matcher, row.sibling} <= matchers, (stem, matchers)


# ---------------------------------------------------------------------------
# 4. end to end: a RESUMED session in a NON-root project
# ---------------------------------------------------------------------------


def _hook_env(tmp_path: Path, project: Path, orch: Path) -> dict:
    """A scratch world: state dir, HOME, no PATH hub, the launcher and the
    gateway legs switched off by their own kill switches (so the live
    launcher/gateway are never touched), vco_lib from THIS checkout."""
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]),
        "LANG": "C.UTF-8",
        "VCT_STATE_DIR": str(tmp_path / "state"),
        "VCT_VENV": sys.prefix,
        "PYTHONPATH": str(REPO),
        "VCT_ORCHESTRATOR_ROOT": str(orch),
        "VCT_DISABLE_LAUNCHER_AUTOSTART": "1",
        "VCT_DISABLE_BOOT_SERVICE": "1",
        "CLAUDE_PROJECT_DIR": str(project),
        "WEAVIATE_URL": "http://127.0.0.1:9",
        "TMPDIR": str(tmp_path / "tmp"),
    }
    for d in ("home", "state", "tmp"):
        (tmp_path / d).mkdir(exist_ok=True)
    return env


def _install_hook(project: Path) -> Path:
    dest = project / ".claude" / "hooks"
    shutil.copytree(HOOKS / "_lib", dest / "_lib", dirs_exist_ok=True)
    hook = dest / f"{SERVICE_HOOK}.sh"
    shutil.copy2(HOOKS / f"{SERVICE_HOOK}.sh", hook)
    return hook


def _run_hook(hook: Path, env: dict, source: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1",
                          "source": source})
    return subprocess.run(["bash", str(hook)], input=payload, capture_output=True,
                          text=True, timeout=120, env=env, cwd=str(hook.parents[2]))


@needs_bash
def test_a_resumed_session_in_a_project_starts_a_stopped_hub(tmp_path: Path) -> None:
    marker = tmp_path / "hub-started"
    orch = tmp_path / "orch"
    _fake_orchestrator(orch, body=f'#!/bin/sh\nprintf "%s\\n" "$@" > "{marker}"\nexit 0\n')
    project = _project(tmp_path / "SomeProject")
    hook = _install_hook(project)

    res = _run_hook(hook, _hook_env(tmp_path, project, orch), "resume")
    assert res.returncode == 0, res.stderr

    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists(), f"the fake hub was never started; stdout={res.stdout!r}"
    assert marker.read_text(encoding="utf-8").split() == ["--start-if-not-running"]
    assert "binary not found" not in res.stdout


@needs_bash
def test_a_resumed_session_leaves_a_running_hub_alone(tmp_path: Path) -> None:
    marker = tmp_path / "hub-started"
    orch = tmp_path / "orch"
    _fake_orchestrator(orch, body=f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    project = _project(tmp_path / "SomeProject")
    hook = _install_hook(project)
    env = _hook_env(tmp_path, project, orch)
    # A live owner pid in the scratch lockfile = "the hub is running".
    (tmp_path / "state" / "hub.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    res = _run_hook(hook, env, "resume")
    assert res.returncode == 0, res.stderr
    time.sleep(1.0)
    assert not marker.exists(), "a running hub must not be spawned again"
    assert res.stdout.strip() == "", res.stdout


@needs_bash
def test_a_hub_that_cannot_be_found_is_reported_as_session_context(tmp_path: Path) -> None:
    """The hook is ASYNC: only JSON additionalContext reaches the session, so
    the not-found report is emitted in that envelope, not as bare text."""
    orch = tmp_path / "orch"
    (orch / "vco_lib").mkdir(parents=True)
    (orch / ".claude").mkdir(parents=True)
    project = _project(tmp_path / "SomeProject")
    hook = _install_hook(project)
    env = _hook_env(tmp_path, project, orch)
    # No dist anywhere the chain looks: point the checkout fallback away too
    # by running the hook against a copy of vco_lib whose parent has no dist.
    shadow = tmp_path / "shadow"
    shutil.copytree(REPO / "vco_lib", shadow / "vco_lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env["PYTHONPATH"] = str(shadow)

    res = _run_hook(hook, env, "resume")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "vct-hub binary not found" in out["additionalContext"]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_the_ps1_sibling_reports_through_the_same_envelope(tmp_path: Path) -> None:
    """Parity: the .ps1 hook collects its reports and emits them once as
    SessionStart additionalContext too."""
    from tests.common.pwsh import PWSH_BASE_FLAGS

    orch = tmp_path / "orch"
    (orch / "vco_lib").mkdir(parents=True)
    (orch / ".claude").mkdir(parents=True)
    project = _project(tmp_path / "SomeProject")
    dest = project / ".claude" / "hooks"
    shutil.copytree(HOOKS / "_lib", dest / "_lib", dirs_exist_ok=True)
    hook = dest / f"{SERVICE_HOOK}.ps1"
    shutil.copy2(HOOKS / f"{SERVICE_HOOK}.ps1", hook)
    env = _hook_env(tmp_path, project, orch)
    env["PATH"] = os.pathsep.join([str(Path(shutil.which("pwsh") or "").parent), "/usr/bin", "/bin"])
    shadow = tmp_path / "shadow"
    shutil.copytree(REPO / "vco_lib", shadow / "vco_lib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    env["PYTHONPATH"] = str(shadow)

    res = subprocess.run(
        [shutil.which("pwsh") or "pwsh", *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(hook)],
        input=json.dumps({"source": "resume"}), capture_output=True, text=True,
        timeout=180, env=env, cwd=str(project))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "vct-hub binary not found" in out["additionalContext"]


# ---------------------------------------------------------------------------
# R29 LOW: the no-interpreter leg reports through the same envelope
# ---------------------------------------------------------------------------


def _broken_install(tmp_path: Path, ext: str) -> tuple[Path, Path, dict]:
    """A project whose hook has NO interpreter: the `_lib/` helpers that find
    one (find-python, resolve-vco-venv) are missing — a broken install."""
    orch = tmp_path / "orch"
    (orch / "vco_lib").mkdir(parents=True)
    (orch / ".claude").mkdir(parents=True)
    project = _project(tmp_path / "SomeProject")
    hook = project / ".claude" / "hooks" / f"{SERVICE_HOOK}.{ext}"
    shutil.copy2(HOOKS / f"{SERVICE_HOOK}.{ext}", hook)
    env = _hook_env(tmp_path, project, orch)
    for var in ("VCT_VENV", "PY", "VCO_VENV_PYTHON"):
        env.pop(var, None)
    return project, hook, env


def _assert_no_python_envelope(stdout: str) -> None:
    out = json.loads(stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "no Python interpreter for vco_lib.hub_ensure" in out["additionalContext"]
    assert "permissionDecision" not in out


@needs_bash
def test_a_hook_with_no_interpreter_reports_as_session_context(tmp_path: Path) -> None:
    """RED before R29: the line was plain stdout, which an async hook's
    session never receives."""
    _project_dir, hook, env = _broken_install(tmp_path, "sh")
    res = _run_hook(hook, env, "resume")
    assert res.returncode == 0, res.stderr
    _assert_no_python_envelope(res.stdout)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_the_ps1_sibling_reports_no_interpreter_through_the_envelope(tmp_path: Path) -> None:
    from tests.common.pwsh import PWSH_BASE_FLAGS

    project, hook, env = _broken_install(tmp_path, "ps1")
    env["PATH"] = os.pathsep.join([str(Path(shutil.which("pwsh") or "").parent), "/usr/bin", "/bin"])
    res = subprocess.run(
        [shutil.which("pwsh") or "pwsh", *PWSH_BASE_FLAGS, "-NonInteractive", "-File", str(hook)],
        input=json.dumps({"source": "resume"}), capture_output=True, text=True,
        timeout=180, env=env, cwd=str(project))
    assert res.returncode == 0, res.stderr
    _assert_no_python_envelope(res.stdout)
