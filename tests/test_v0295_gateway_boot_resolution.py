# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The gateway's boot registration resolves — and PROVES — its entry point.

v0.2.95 R5a/R5b, from a live defect with two halves that hid each other:

* **R5a** — ``resolve_gateway_exec`` keyed off ``sys.executable``, so
  ``install.py --update`` baked whichever interpreter ran the installer. On
  2026-09-10 that was a system python which cannot import ``model_router``:
  the unit was unrunnable from the moment it was written, and it stayed
  invisible for eight hours because the previous process kept serving.
* **R5b** — the unit's working directory is the state root, which is not a
  registered project, so the secrets resolver skipped its hub tier (the only
  route to an OS-keychain key) and every vendor request answered "no key".

Tri-OS is asserted by passing ``os_key`` / ``system`` explicitly rather than
branching on the host, the way ``test_model_gateway_boot.py`` does. Nothing
here touches a real init system, a real venv or the developer's home: the
sandbox redirects both roots and every candidate is a file this test made.

Every gate has an ACT test and a LEAVE-ALONE test, and the leave-alone proves
the neighbouring artefact survives BY HASH — "refused to write" and "wrote
nothing" are different claims.
"""
from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from vco_lib import boot_service as bs  # noqa: E402

ALL_OS = ("Linux", "Darwin", "Windows")


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Redirect the user home AND the state root, and make every init tool
    absent — prevention, not recovery (the 2026-05-16 incident)."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(home))
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv("VCT_DISABLE_BOOT_SERVICE", raising=False)
    monkeypatch.delenv("VCT_MODEL_GATEWAY_SECRET_PROJECT", raising=False)
    monkeypatch.delenv("VCT_VENV", raising=False)
    monkeypatch.setattr(bs.shutil, "which", lambda name: None)
    return home, state


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_clone(root: Path, *, console_script: bool = True) -> Path:
    """A directory that `looks_like_orchestrator_root` and holds a venv."""
    (root / "vco_lib").mkdir(parents=True, exist_ok=True)
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    bindir = root / ".venv" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "python").write_text("", encoding="utf-8")
    if console_script:
        (bindir / "vct-model-gateway").write_text("#!/bin/sh\n", encoding="utf-8")
    return root


def _stranger_python(root: Path) -> Path:
    """An interpreter with NO gateway console script beside it — the system
    python that produced the field defect."""
    bindir = root / "usr" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "python3.12"
    exe.write_text("", encoding="utf-8")
    return exe


def _answers(ok_for: str):
    """A verification runner that only lets ``ok_for`` answer ``--version``."""

    def run(cmd, timeout):
        return (0, "") if str(cmd[0]) == ok_for else (1, "No module named 'model_router'")

    return run


def _never_answers(cmd, timeout):
    return 1, "No module named 'model_router'"


def _fake_tools(monkeypatch) -> list:
    """Make every init-system tool 'present' and record its invocations.

    The same seam ``test_model_gateway_boot.py`` uses, for the same reason: it
    lets one box assert the SHAPE of all three mechanisms. It does not make the
    box able to APPLY a launchd plist or import a Windows task — that
    acceptance is an integration gap, named as one.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(bs.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(argv, *a, **kw):
        calls.append([str(x) for x in argv])
        return _Done()

    monkeypatch.setattr(bs.subprocess, "run", run)
    return calls


# ---------------------------------------------------------------------------
# R5a — resolution comes from the INSTALL ROOT, not from sys.executable
# ---------------------------------------------------------------------------


def test_the_argv_names_the_install_venv_not_the_running_interpreter(
    tmp_path, monkeypatch,
):
    """THE field defect. Rendered from a process whose `sys.executable` has no
    console script beside it, the argv must still name the install venv."""
    clone = _fake_clone(tmp_path / "clone")
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))

    argv = bs.resolve_gateway_exec(install_root=clone)

    assert argv == [str(clone / ".venv" / "bin" / "vct-model-gateway")]
    assert "usr/bin/python3.12" not in " ".join(argv)


def test_the_module_form_uses_the_install_venv_python_when_no_script_exists(
    tmp_path, monkeypatch,
):
    clone = _fake_clone(tmp_path / "clone", console_script=False)
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))

    assert bs.resolve_gateway_exec(install_root=clone) == [
        str(clone / ".venv" / "bin" / "python"), "-m", "model_router",
    ]


def test_the_dotted_package_form_is_never_used(tmp_path, monkeypatch):
    """`claude_mcp_servers/` has no `__init__.py`, so the dotted form only ever
    resolved from the repo root — which a daemon must not assume it is in."""
    clone = _fake_clone(tmp_path / "clone", console_script=False)
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))
    for argv in bs.gateway_exec_candidates(install_root=clone):
        assert "claude_mcp_servers.model_router" not in " ".join(argv)


def test_sys_executable_is_the_last_rung_and_still_offered(tmp_path, monkeypatch):
    """LEAVE-ALONE for the broken-install case: when no venv resolves, the
    running interpreter is still a candidate — verification decides, not the
    ordering."""
    stranger = _stranger_python(tmp_path)
    monkeypatch.setattr(sys, "executable", str(stranger))
    clone = _fake_clone(tmp_path / "clone")

    candidates = bs.gateway_exec_candidates(install_root=clone)

    assert candidates[0] == [str(clone / ".venv" / "bin" / "vct-model-gateway")]
    assert candidates[-1] == [str(stranger), "-m", "model_router"]


def test_the_vct_venv_override_wins(tmp_path, monkeypatch):
    clone = _fake_clone(tmp_path / "clone")
    override = _fake_clone(tmp_path / "override")
    monkeypatch.setenv("VCT_VENV", str(override / ".venv"))
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))

    assert bs.resolve_gateway_exec(install_root=clone) == [
        str(override / ".venv" / "bin" / "vct-model-gateway"),
    ]


# ---------------------------------------------------------------------------
# R5a — VERIFY before baking: `--version` is run, and a failure refuses
# ---------------------------------------------------------------------------


def test_verification_runs_the_candidate_with_the_version_flag(tmp_path, monkeypatch):
    clone = _fake_clone(tmp_path / "clone")
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))
    seen = []

    def run(cmd, timeout):
        seen.append([str(c) for c in cmd])
        return 0, ""

    resolved = bs.resolve_gateway_exec_verified(install_root=clone, runner=run)

    assert resolved.verified is True
    assert seen[0][-1] == "--version"
    assert resolved.argv == (str(clone / ".venv" / "bin" / "vct-model-gateway"),)


def test_a_candidate_that_cannot_import_the_package_is_skipped(tmp_path, monkeypatch):
    """ACT: the console script is broken, the module form answers — the module
    form is what gets baked."""
    clone = _fake_clone(tmp_path / "clone")
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))
    venv_python = str(clone / ".venv" / "bin" / "python")

    resolved = bs.resolve_gateway_exec_verified(
        install_root=clone, runner=_answers(venv_python),
    )

    assert resolved.verified is True
    assert resolved.argv == (venv_python, "-m", "model_router")
    assert resolved.tried, "the failed candidate must be NAMED, not forgotten"


def test_when_nothing_answers_the_resolution_is_a_refusal_with_no_argv(
    tmp_path, monkeypatch,
):
    clone = _fake_clone(tmp_path / "clone")
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))

    resolved = bs.resolve_gateway_exec_verified(
        install_root=clone, runner=_never_answers,
    )

    assert resolved.verified is False
    assert resolved.argv == ()
    assert "install.py --update" in resolved.reason
    assert len(resolved.tried) >= 2


def test_an_unspawnable_candidate_is_told_apart_from_one_that_ran_and_failed(
    tmp_path,
):
    def explodes(cmd, timeout):
        raise OSError("No such file or directory")

    ok, why = bs.verify_gateway_exec(["/gone/gw"], runner=explodes)
    assert ok is False
    assert "OSError" in why


@pytest.mark.parametrize("os_key", ALL_OS)
def test_a_refusal_writes_no_registration_at_all(tmp_path, monkeypatch, os_key):
    """ACT (the whole point of R5a): an unrunnable entry point produces NO
    artefact. A unit that cannot start is worse than none — it is enabled, so
    the init system keeps retrying it while the toggle reads `registered`."""
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))

    result = bs.register_model_gateway(
        templates_root=REPO_ROOT, system=os_key, home=home,
        install_root=clone, state_dir=state, runner=_never_answers,
    )

    assert result.refused is True
    assert result.registered is False
    assert result.spec is None
    assert _artefact(os_key, home, state) is None or not _artefact(
        os_key, home, state,
    ).exists()


@pytest.mark.parametrize("os_key", ALL_OS)
def test_a_refusal_leaves_an_existing_registration_byte_identical(
    tmp_path, monkeypatch, os_key,
):
    """LEAVE-ALONE: the machine already has a (possibly broken) unit. A
    refusal must not replace it with a second broken one, and must not touch
    the user's bytes at all."""
    home, state = _homes(monkeypatch, tmp_path)
    _fake_tools(monkeypatch)
    clone = _fake_clone(tmp_path / "clone")
    ok = bs.register_model_gateway(
        templates_root=REPO_ROOT, system=os_key, home=home, install_root=clone,
        state_dir=state, runner=_answers(str(clone / ".venv" / "bin" / "vct-model-gateway")),
    )
    assert ok.registered is True
    artefact = _artefact(os_key, home, state)
    before = _sha(artefact)

    monkeypatch.setattr(sys, "executable", str(_stranger_python(tmp_path)))
    refused = bs.register_model_gateway(
        templates_root=REPO_ROOT, system=os_key, home=home, install_root=clone,
        state_dir=state, runner=_never_answers, update_only=True,
    )

    assert refused.refused is True
    assert _sha(artefact) == before


@pytest.mark.parametrize("os_key", ALL_OS)
def test_the_verified_argv_is_what_lands_in_the_artefact(tmp_path, monkeypatch, os_key):
    home, state = _homes(monkeypatch, tmp_path)
    _fake_tools(monkeypatch)
    clone = _fake_clone(tmp_path / "clone", console_script=False)
    venv_python = str(clone / ".venv" / "bin" / "python")

    result = bs.register_model_gateway(
        templates_root=REPO_ROOT, system=os_key, home=home, install_root=clone,
        state_dir=state, runner=_answers(venv_python),
    )

    assert result.registered is True
    facts = bs.installed_gateway_facts(home=home, system=os_key, state_dir=state)
    assert facts.argv == (venv_python, "-m", "model_router", "serve")


def test_the_update_leg_creates_nothing_on_a_machine_that_never_opted_in(
    tmp_path, monkeypatch,
):
    """LEAVE-ALONE, and the security-relevant half: an update that quietly
    turned a login-time OAuth-bearing daemon on would be making the user's
    decision for them. It must not even RUN the verification."""
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    spawned = []

    def record(cmd, timeout):
        spawned.append(cmd)
        return 0, ""

    result = bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=record, update_only=True,
    )

    assert result.registered is False and result.refused is False
    assert spawned == [], "a machine with no registration must pay no subprocess"
    assert not bs.systemd_unit_path(
        bs.gateway_names_spec("Linux", state_dir=state), home,
    ).exists()


def test_the_kill_switch_refuses_before_anything_is_resolved(tmp_path, monkeypatch):
    home, state = _homes(monkeypatch, tmp_path)
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    spawned = []

    result = bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, state_dir=state,
        runner=lambda cmd, timeout: spawned.append(cmd) or (0, ""),
    )

    assert result.registered is False and result.refused is False
    assert spawned == []


def test_install_py_rerender_passes_the_verified_argv(monkeypatch, tmp_path):
    """The WIRING, not just the helper: `--update` must go through the
    verifying path, and a plain install must not touch the registration."""
    import argparse

    import install  # type: ignore

    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    seen: list = []

    def fake_register(**kwargs):
        seen.append(kwargs)
        return bs.GatewayRegistration(
            registered=True, refused=False,
            exec_result=bs.GatewayExec(argv=(script,), verified=True),
            spec=None, reason="",
        )

    monkeypatch.setattr(install._boot_service, "register_model_gateway", fake_register)
    install._rerender_model_gateway_boot_service(argparse.Namespace(update=False))
    assert seen == []

    install._rerender_model_gateway_boot_service(argparse.Namespace(update=True))
    assert len(seen) == 1
    assert seen[0]["update_only"] is True
    assert seen[0]["templates_root"] == install.PROJECT_ROOT
    assert seen[0]["install_root"] == install.PROJECT_ROOT


def test_install_py_reports_a_refusal_instead_of_swallowing_it(monkeypatch, tmp_path):
    import argparse

    import install  # type: ignore

    monkeypatch.setattr(
        install._boot_service, "register_model_gateway",
        lambda **kw: bs.GatewayRegistration(
            registered=False, refused=True,
            exec_result=bs.GatewayExec(argv=(), verified=False),
            spec=None, reason="nothing answered --version",
        ),
    )
    logged: list = []
    monkeypatch.setattr(
        install, "_log_install_event",
        lambda phase, level, detail, *a, **k: logged.append((level, detail)),
    )

    install._rerender_model_gateway_boot_service(argparse.Namespace(update=True))

    assert any(
        level == "warn" and "nothing answered --version" in detail
        for level, detail in logged
    ), logged


# ---------------------------------------------------------------------------
# R5b + Q3 — the unit pins NOTHING, and the daemon resolves its own scope
# ---------------------------------------------------------------------------
#
# R5b shipped the opposite first: it BAKED the install root into the unit,
# because a boot unit's cwd (the state root) is not a registered project and
# the secrets chain resolved `project or cwd()`. Q3 then fixed that in the
# daemon — an unpinned gateway resolves this install's orchestrator root at
# runtime — which reaches every start path, not only a rendered unit. Keeping
# the bake after that would have DEFEATED the runtime default wherever the
# gateway is boot-started (`scope_origin() == "pin"` for a pin nobody set) and
# frozen a path that a moved install could never heal. So these tests pin the
# EMPTY render, the preservation of a scope somebody chose, and the one
# carve-out that keeps an updating machine from inheriting the old bake.


@pytest.mark.parametrize("os_key", ALL_OS)
def test_every_os_renders_no_secret_scope_at_all(tmp_path, monkeypatch, os_key):
    """A fresh registration derives NOTHING — the daemon decides at runtime."""
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")

    bs.register_model_gateway(
        templates_root=REPO_ROOT, system=os_key, home=home, install_root=clone,
        state_dir=state, runner=_answers(script),
    )

    facts = bs.installed_gateway_facts(home=home, system=os_key, state_dir=state)
    assert facts.secret_project is None, (
        "the unit baked a derived scope; the daemon would then report "
        "scope_origin()=='pin' for a pin the user never set, and a moved "
        "install would keep naming the old path"
    )


def test_an_explicit_pin_outranks_the_install_root(tmp_path, monkeypatch):
    clone = _fake_clone(tmp_path / "clone")
    assert bs.resolve_gateway_secret_project(
        env={"VCT_MODEL_GATEWAY_SECRET_PROJECT": "Acme"}, install_root=clone,
    ) == "Acme"


def test_a_scope_already_baked_in_is_preserved_by_a_re_render(tmp_path, monkeypatch):
    """LEAVE-ALONE: a user who pinned a scope must not have it silently
    replaced — or dropped — by a re-render."""
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[script], state_dir=state,
        secret_project="TheUsersOwnProject",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)

    bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=_answers(script), update_only=True,
    )

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)
    assert facts.secret_project == "TheUsersOwnProject"


@pytest.mark.parametrize("suffix", ["", "/"])
def test_a_re_render_drops_a_value_that_only_equals_this_installs_root(
    tmp_path, monkeypatch, suffix,
):
    """The updating machine's case — and the reason the carve-out exists.

    A 0.2.95-pre-fix render, and the hand-written systemd drop-in this
    release replaces, both left the install root in the artefact. Rung 2
    cannot ask who wrote it, but it CAN ask whether it says anything the
    runtime default would not: when it does not, dropping it changes no
    behaviour today and lets the default self-heal after a move. Without this,
    `install.py --update` would carry the frozen path forward for life.

    The trailing-separator arm is why the comparison is `Path`-based rather
    than a string compare.
    """
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[script], state_dir=state,
        secret_project=str(clone) + suffix,
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    assert bs.installed_gateway_facts(
        home=home, system="Linux", state_dir=state,
    ).secret_project == str(clone) + suffix

    bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=_answers(script), update_only=True,
    )

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)
    assert facts.secret_project is None


def test_a_0294_unit_with_no_scope_line_updates_to_an_empty_assignment(
    tmp_path, monkeypatch,
):
    """What the 0.2.95 update leaves on a machine that opted in under 0.2.94.

    A 0.2.94 unit has no `Environment=VCT_MODEL_GATEWAY_SECRET_PROJECT` line
    at all — the variable did not exist. `install.py --update` re-renders it,
    and the end state has to be the EMPTY assignment (= unset = the runtime
    default decides), never a derived path.
    """
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[script], state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    unit = bs.systemd_unit_path(spec, home)
    unit.write_text(
        "\n".join(
            line for line in unit.read_text(encoding="utf-8").splitlines()
            if bs.GATEWAY_SECRET_PROJECT_ENV not in line
        ) + "\n",
        encoding="utf-8",
    )
    assert bs.GATEWAY_SECRET_PROJECT_ENV not in unit.read_text(encoding="utf-8")

    bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=_answers(script), update_only=True,
    )

    body = unit.read_text(encoding="utf-8")
    assert f"Environment={bs.GATEWAY_SECRET_PROJECT_ENV}=\n" in body + "\n"
    assert bs.installed_gateway_facts(
        home=home, system="Linux", state_dir=state,
    ).secret_project is None


def test_a_hand_written_drop_in_is_not_absorbed_into_the_unit(tmp_path, monkeypatch):
    """The maintainer's machine, exactly: a drop-in pinning the install root.

    `installed_gateway_facts` folds drop-ins in (systemd runs them), so rung 2
    SEES that value on the 0.2.95 update. Absorbing it into the main unit
    would outlive the drop-in's deletion and freeze the path for good — the
    carve-out is what stops that. The drop-in itself is untouched: it is the
    user's file, it still governs the running daemon until they remove it, and
    when they do, the runtime default answers with the same path.
    """
    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[script], state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    unit = bs.systemd_unit_path(spec, home)
    dropin = unit.with_name(unit.name + ".d") / "secret-project.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text(
        f"[Service]\nEnvironment={bs.GATEWAY_SECRET_PROJECT_ENV}={clone}\n",
        encoding="utf-8",
    )
    dropin_hash = _sha(dropin)

    bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=_answers(script), update_only=True,
    )

    assert _rendered_env_assignment(
        unit.read_text(encoding="utf-8"), bs.GATEWAY_SECRET_PROJECT_ENV,
    ) == ""
    assert _sha(dropin) == dropin_hash, "the user's drop-in was rewritten"


def test_nothing_is_pinned_whether_or_not_a_clone_resolves(tmp_path, monkeypatch):
    """Both rung-3 arms answer the same way: a fresh registration pins nothing.

    The no-clone arm is the older of the two — it existed so a machine with
    no resolvable clone would not have a GUESS baked into it. It now agrees
    with the resolvable case, which is the point.
    """
    clone = _fake_clone(tmp_path / "clone")
    assert bs.resolve_gateway_secret_project(env={}, install_root=clone) == ""

    monkeypatch.delenv("VCT_INSTALL_ROOT", raising=False)
    monkeypatch.delenv("VCT_ORCHESTRATOR_ROOT", raising=False)
    monkeypatch.setattr(
        "vco_lib.python_exe.resolve_install_root", lambda explicit=None: None,
    )
    assert bs.resolve_gateway_secret_project(env={}) == ""


def _rendered_env_assignment(unit_text: str, key: str) -> str:
    """The literal value systemd would export for `key` (last assignment wins)."""
    value = None
    for line in unit_text.splitlines():
        stripped = line.strip()
        prefix = f"Environment={key}="
        if stripped.startswith(prefix):
            value = stripped[len(prefix):]
    assert value is not None, f"{key} is not assigned in the rendered unit"
    return value


def test_a_freshly_rendered_unit_leaves_the_runtime_default_in_force(
    tmp_path, monkeypatch,
):
    """END TO END: render → the literal the unit exports → the daemon's own
    config reader → the resolver's verdict.

    Renders through the real registrar, reads the literal back out of the
    artefact, and drives `GatewayConfig.from_env` + `VendorKeyResolver` with
    it — the two objects the daemon builds at startup. `install_root` is the
    ONLY thing stubbed (the resolver must not walk this machine's real
    clone), so the seam under test is the one the review found: a rendered
    unit that answers `pin` has killed the runtime default.
    """
    from model_router.config import GatewayConfig  # noqa: PLC0415
    from model_router.secrets import VendorKeyResolver  # noqa: PLC0415

    home, state = _homes(monkeypatch, tmp_path)
    clone = _fake_clone(tmp_path / "clone")
    script = str(clone / ".venv" / "bin" / "vct-model-gateway")
    bs.register_model_gateway(
        templates_root=REPO_ROOT, system="Linux", home=home, install_root=clone,
        state_dir=state, runner=_answers(script),
    )
    unit = bs.systemd_unit_path(
        bs.gateway_names_spec("Linux", state_dir=state), home,
    ).read_text(encoding="utf-8")

    exported = _rendered_env_assignment(unit, bs.GATEWAY_SECRET_PROJECT_ENV)
    assert exported == ""

    monkeypatch.setenv(bs.GATEWAY_SECRET_PROJECT_ENV, exported)
    config = GatewayConfig.from_env()
    assert config.secret_project is None

    keys = VendorKeyResolver(
        project=config.secret_project, install_root=lambda: str(clone),
    )
    assert keys.scope_origin() == "install_root"
    assert keys.effective_project == str(clone)

    # COUNTER-CASE: the same chain with a scope somebody chose reports `pin`,
    # so the assertion above is about the empty render and not about a
    # resolver that can only ever say one thing.
    monkeypatch.setenv(bs.GATEWAY_SECRET_PROJECT_ENV, "/somebody/elses/project")
    pinned = GatewayConfig.from_env()
    assert pinned.secret_project == "/somebody/elses/project"
    assert VendorKeyResolver(
        project=pinned.secret_project, install_root=lambda: str(clone),
    ).scope_origin() == "pin"


def test_an_empty_scope_renders_as_an_unset_variable(tmp_path, monkeypatch):
    """A pin nobody made must read back as `None`, i.e. the pre-v0.2.95
    behaviour — never as a literal empty project name."""
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=["/gw"], state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)
    assert facts.secret_project is None


# ---------------------------------------------------------------------------
# The read-back itself — three artefact shapes, one parser family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("os_key", ALL_OS)
def test_the_installed_argv_is_read_back_from_the_artefact(
    tmp_path, monkeypatch, os_key,
):
    """Re-deriving what we WOULD write answers a different question from
    reading what the machine RUNS — and only the second could have caught
    2026-09-10."""
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.model_gateway_spec(
        os_key=os_key, exec_argv=["/opt/vco/bin/gw", "--flag"], state_dir=state,
        secret_project="/opt/vco",
    )
    bs.register(spec, templates_root=REPO_ROOT, system=os_key, home=home)

    facts = bs.installed_gateway_facts(home=home, system=os_key, state_dir=state)

    assert facts.exists is True
    assert facts.argv == ("/opt/vco/bin/gw", "--flag", "serve")
    assert facts.parse_error is None


@pytest.mark.parametrize("os_key", ALL_OS)
def test_an_absent_artefact_is_absence_not_a_parse_failure(
    tmp_path, monkeypatch, os_key,
):
    home, state = _homes(monkeypatch, tmp_path)
    facts = bs.installed_gateway_facts(home=home, system=os_key, state_dir=state)
    assert facts.exists is False
    assert facts.argv == ()
    assert facts.parse_error is None


def test_a_path_with_a_space_survives_the_round_trip(tmp_path, monkeypatch):
    home, state = _homes(monkeypatch, tmp_path)
    exe = "/opt/my clone/.venv/bin/vct-model-gateway"
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[exe], state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)
    assert facts.argv == (exe, "serve")


def test_a_drop_in_that_overrides_the_command_is_what_gets_reported(
    tmp_path, monkeypatch,
):
    """systemd runs the unit PLUS its drop-ins, so reading the unit alone
    reports a command this machine does not run — and would call a working
    gateway "unrunnable". The maintainer's own machine carried exactly such a
    drop-in while this was written."""
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=["/opt/old/gw"], state_dir=state,
        secret_project="/opt/old",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    unit = bs.systemd_unit_path(spec, home)
    dropin = unit.with_name(unit.name + ".d") / "override.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text(
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=/opt/new/venv/bin/vct-model-gateway serve\n"
        "Environment=VCT_MODEL_GATEWAY_SECRET_PROJECT=/opt/new\n",
        encoding="utf-8",
    )

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)

    assert facts.argv == ("/opt/new/venv/bin/vct-model-gateway", "serve")
    assert facts.secret_project == "/opt/new"
    assert facts.parse_error is None


def test_drop_ins_are_applied_in_systemds_own_order(tmp_path, monkeypatch):
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=["/opt/old/gw"], state_dir=state, secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    unit = bs.systemd_unit_path(spec, home)
    directory = unit.with_name(unit.name + ".d")
    directory.mkdir(parents=True)
    (directory / "10-first.conf").write_text(
        "[Service]\nExecStart=\nExecStart=/first/gw serve\n", encoding="utf-8",
    )
    (directory / "20-second.conf").write_text(
        "[Service]\nExecStart=\nExecStart=/second/gw serve\n", encoding="utf-8",
    )

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)

    assert facts.argv == ("/second/gw", "serve")


def test_a_unit_with_no_drop_ins_is_unaffected(tmp_path, monkeypatch):
    """LEAVE-ALONE: the common case reads exactly as before."""
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=["/opt/vco/bin/gw"], state_dir=state,
        secret_project="",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)

    assert facts.argv == ("/opt/vco/bin/gw", "serve")


def test_a_hand_broken_unit_reports_WHY_it_yielded_no_argv(tmp_path, monkeypatch):
    home, state = _homes(monkeypatch, tmp_path)
    spec = bs.gateway_names_spec("Linux", state_dir=state)
    unit = bs.systemd_unit_path(spec, home)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n# the user deleted the ExecStart\n", encoding="utf-8")

    facts = bs.installed_gateway_facts(home=home, system="Linux", state_dir=state)

    assert facts.exists is True
    assert facts.argv == ()
    assert facts.parse_error and "ExecStart" in facts.parse_error


def test_the_names_spec_resolves_no_entry_point_at_all(tmp_path, monkeypatch):
    """A caller that only inspects must not pay for — or depend on — the
    resolution a registration needs."""
    calls = []
    monkeypatch.setattr(
        bs, "resolve_gateway_exec",
        lambda **kw: calls.append(kw) or ["should-not-be-called"],
    )
    bs.gateway_names_spec("Linux")
    assert calls == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _homes(monkeypatch, tmp_path):
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(home))
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    return home, state


def _artefact(os_key: str, home: Path, state: Path):
    spec = bs.gateway_names_spec(os_key, state_dir=state)
    if os_key == "Linux":
        return bs.systemd_unit_path(spec, home)
    if os_key == "Darwin":
        return bs.launchd_plist_path(spec, home)
    return spec.windows_task_xml_path


def test_the_host_os_is_never_what_decides_a_test_here():
    """The suite asserts all three shapes on whatever box runs it; if a test
    ever starts branching on the host, this is the reminder that it must not."""
    assert platform.system() in ("Linux", "Darwin", "Windows", "")
