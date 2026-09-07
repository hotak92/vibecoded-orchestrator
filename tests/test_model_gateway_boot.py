# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Model-gateway boot registration and its inverse (v0.2.92 WP-10).

TRI-OS IS THE POINT OF THIS PACKAGE, so all three registration mechanisms
are real and the SHAPE of each is asserted here — on whatever host runs the
suite, by passing ``os_key`` / ``system`` explicitly instead of branching on
``platform.system()``. What a Linux CI box CANNOT do is APPLY a launchd
plist or import a Windows Scheduled Task, so ``launchctl bootstrap`` and
``schtasks /Create`` acceptance stay integration gaps named in the report
rather than coverage implied by a green run here.

Three axes, each named in a test:
  * fresh          — nothing registered; the CLI flag registers.
  * update         — an existing registration is re-rendered, and a machine
                     with none gets none.
  * already-damaged— a unit pointing at a path that no longer exists, and a
                     state directory holding a `.pre-vco` backup.

Every deletion branch has an ACT test and a LEAVE-ALONE test, and each
leave-alone proves a NEIGHBOURING file survives by hash — because "removed
the right thing" and "removed only the right thing" are different claims.
"""
from __future__ import annotations

import ast
import hashlib
import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from vco_lib import boot_service as bs  # noqa: E402

TEMPLATES = REPO_ROOT / "templates"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Redirect the user home AND the state root. Prevention, not recovery:
    a boot test that forgets one of these rewrites the developer's own
    systemd unit (the 2026-05-16 incident)."""
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(home))
    monkeypatch.setenv("VCT_STATE_DIR", str(state))
    monkeypatch.delenv("VCT_DISABLE_BOOT_SERVICE", raising=False)
    # Never let a real systemctl/launchctl/schtasks be found.
    monkeypatch.setattr(bs.shutil, "which", lambda name: None)
    return home, state


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_without_prose(path: Path) -> str:
    """Source with comments AND docstrings removed.

    A "this file must not contain X" test that reads raw bytes flags the
    very docstring explaining why X is not there — which teaches the next
    author to delete the explanation rather than keep the property.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    drop: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return "\n".join(
        line for i, line in enumerate(lines, 1)
        if i not in drop and not line.lstrip().startswith("#")
    )


def _fake_tools(monkeypatch) -> list:
    """Make every init-system tool 'present' and record its invocations.

    This is what lets a Linux CI box assert the SHAPE of all three
    mechanisms. It does not make the box able to APPLY a launchd plist or a
    Windows task — that acceptance is an integration gap, named as one.
    """
    calls: list[list[str]] = []  # noqa: F841 — returned below
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
# FRESH — the three registration mechanisms, one per OS
# ---------------------------------------------------------------------------


def test_fresh_linux_writes_a_systemd_user_unit_and_enables_it(
    sandbox, monkeypatch,
):
    home, state = sandbox
    calls = _fake_tools(monkeypatch)
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/opt/vco/bin/gw"])
    assert bs.register(
        spec, templates_root=REPO_ROOT, system="Linux", home=home,
    ) is True

    unit = bs.systemd_unit_path(spec, home)
    body = unit.read_text(encoding="utf-8")
    assert "[Install]" in body and "WantedBy=default.target" in body
    assert "ExecStart='/opt/vco/bin/gw' 'serve'" in body
    assert f"Environment=VCT_STATE_DIR={state}" in body
    assert "Restart=on-failure" in body
    assert "{{" not in body

    flat = [" ".join(c) for c in calls]
    assert any("--user daemon-reload" in c for c in flat)
    # `--now` because a user who asked for autostart expects it running now,
    # the same decision `vct-hub --register-boot` makes. The container stack
    # deliberately does NOT pass it.
    assert any(
        f"--user enable --now {bs.MODEL_GATEWAY_UNIT_NAME}" in c for c in flat
    ), flat
    assert any("enable-linger" in c for c in flat)


def test_the_container_stack_is_still_enabled_without_now(sandbox, monkeypatch):
    """LEAVE-ALONE for the shared `enable_now` knob: the extraction must not
    have started the container stack a second time behind compose's back."""
    home, _ = sandbox
    calls = _fake_tools(monkeypatch)
    install_path = home / "clone"
    (install_path / "scripts").mkdir(parents=True)
    (install_path / "claude_mcp_servers").mkdir()
    spec = bs.container_stack_spec(
        install_path, install_path / "claude_mcp_servers", os_key="Linux",
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    flat = [" ".join(c) for c in calls]
    assert any(f"--user enable {bs.CONTAINER_STACK_UNIT_NAME}" in c for c in flat)
    assert not any("--now" in c for c in flat), flat


def test_fresh_macos_writes_a_launchagent_plist_and_bootstraps_it(
    sandbox, monkeypatch,
):
    home, state = sandbox
    calls = _fake_tools(monkeypatch)
    spec = bs.model_gateway_spec(os_key="Darwin", exec_argv=["/opt/vco/bin/gw"])
    assert bs.register(
        spec, templates_root=REPO_ROOT, system="Darwin", home=home,
    ) is True

    plist = bs.launchd_plist_path(spec, home)
    body = plist.read_text(encoding="utf-8")
    assert "{{" not in body
    root = ET.fromstring(body)
    strings = [e.text for e in root.iter("string")]
    assert bs.MODEL_GATEWAY_PLIST_LABEL in strings
    assert "/opt/vco/bin/gw" in strings and "serve" in strings
    assert str(state) in strings

    flat = [" ".join(c) for c in calls]
    assert any("launchctl bootstrap gui/" in c for c in flat), flat
    assert any("launchctl kickstart -k" in c for c in flat), flat
    # launchd refuses a job whose StandardOutPath directory is missing.
    assert (state / "logs").is_dir()


def test_fresh_windows_writes_and_imports_an_importable_task_xml(
    sandbox, monkeypatch,
):
    home, state = sandbox
    calls = _fake_tools(monkeypatch)
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "tester")
    spec = bs.model_gateway_spec(
        os_key="Windows", exec_argv=[r"C:\vco\Scripts\vct-model-gateway.exe"],
    )
    assert bs.register(
        spec, templates_root=REPO_ROOT, system="Windows", home=home,
    ) is True

    flat = [" ".join(c) for c in calls]
    assert any(
        f"/Create /TN {bs.MODEL_GATEWAY_TASK_NAME} /XML" in c and c.endswith("/F")
        for c in flat
    ), flat
    assert any(f"/Run /TN {bs.MODEL_GATEWAY_TASK_NAME} /I" in c for c in flat), flat

    xml_path = spec.windows_task_xml_path
    body = xml_path.read_text(encoding="utf-8")
    assert "{{" not in body, "an unsubstituted placeholder would ship literally"
    root = ET.fromstring(body)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:UserId", ns).text == "WORKGROUP\\tester"
    assert root.find(".//t:Command", ns).text == "cmd.exe"
    arguments = root.find(".//t:Arguments", ns).text
    assert f"set VCT_STATE_DIR={str(state)}" in arguments
    assert "vct-model-gateway.exe" in arguments and "serve" in arguments
    assert ">>" in arguments and "2>&1" in arguments


def test_the_xml_is_written_even_when_schtasks_is_missing(sandbox):
    """Soft-fail with an audit artefact: an operator on a machine without
    schtasks can still see exactly what would have been registered."""
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Windows", exec_argv=["gw.exe"])
    events = []
    bs.register(
        spec, templates_root=REPO_ROOT, system="Windows", home=home,
        on_event=lambda p, d, data=None: events.append((p, d)),
    )
    assert spec.windows_task_xml_path.exists()
    assert any("schtasks not on PATH" in d for _, d in events)


def test_an_unsupported_os_is_a_skip_not_a_crash(sandbox):
    home, _ = sandbox
    events = []
    assert bs.register(
        bs.model_gateway_spec(os_key="Linux"),
        templates_root=REPO_ROOT, system="Haiku", home=home,
        on_event=lambda p, d, data=None: events.append((p, d)),
    ) is False
    assert any("unsupported OS" in d for _, d in events)


def test_the_boot_log_and_the_daemon_log_are_different_files(sandbox):
    """Otherwise every gateway log record is written twice — once by the
    daemon's file handler, once by the init system capturing its stderr."""
    home, state = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    body = bs.systemd_unit_path(spec, home).read_text(encoding="utf-8")
    daemon_log = state / "logs" / "model-gateway.log"
    boot_log = state / "logs" / "model-gateway.boot.log"
    assert f"append:{boot_log}" in body
    assert f"append:{daemon_log}" not in body


def test_registration_is_refused_when_the_kill_switch_is_set(sandbox, monkeypatch):
    home, _ = sandbox
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    assert bs.register(
        spec, templates_root=REPO_ROOT, system="Linux", home=home,
    ) is False
    assert not bs.systemd_unit_path(spec, home).exists()


# ---------------------------------------------------------------------------
# UPDATE — re-render an existing registration, create none
# ---------------------------------------------------------------------------


def test_update_rerenders_an_existing_registration(sandbox):
    """ACT. The clone moved, so the baked ExecStart is stale; `--update`
    refreshes it in place."""
    home, _ = sandbox
    stale = bs.model_gateway_spec(os_key="Linux", exec_argv=["/old/clone/bin/gw"])
    bs.register(stale, templates_root=REPO_ROOT, system="Linux", home=home)
    unit = bs.systemd_unit_path(stale, home)
    assert "/old/clone/bin/gw" in unit.read_text(encoding="utf-8")

    fresh = bs.model_gateway_spec(os_key="Linux", exec_argv=["/new/clone/bin/gw"])
    assert bs.rerender_if_registered(
        fresh, templates_root=REPO_ROOT, system="Linux", home=home,
    ) is True
    body = unit.read_text(encoding="utf-8")
    assert "/new/clone/bin/gw" in body
    assert "/old/clone/bin/gw" not in body


def test_update_creates_nothing_when_the_user_never_opted_in(sandbox):
    """LEAVE-ALONE, and the security-relevant half: an update that quietly
    turned a login-time OAuth-bearing daemon on would be making the user's
    decision for them."""
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    unit = bs.systemd_unit_path(spec, home)
    assert not unit.exists()
    assert bs.rerender_if_registered(
        spec, templates_root=REPO_ROOT, system="Linux", home=home,
    ) is False
    assert not unit.exists()


def test_update_does_nothing_while_the_kill_switch_is_set(sandbox, monkeypatch):
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/old/gw"])
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    before = _sha(bs.systemd_unit_path(spec, home))
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    assert bs.rerender_if_registered(
        bs.model_gateway_spec(os_key="Linux", exec_argv=["/new/gw"]),
        templates_root=REPO_ROOT, system="Linux", home=home,
    ) is False
    assert _sha(bs.systemd_unit_path(spec, home)) == before


def test_install_py_only_rerenders_on_update(monkeypatch):
    """The wiring, not just the helper: a plain install must not touch it."""
    import argparse

    import install  # type: ignore

    seen = []
    monkeypatch.setattr(
        install._boot_service, "rerender_if_registered",
        lambda *a, **k: seen.append(k) or False,
    )
    install._rerender_model_gateway_boot_service(argparse.Namespace(update=False))
    assert seen == []
    install._rerender_model_gateway_boot_service(argparse.Namespace(update=True))
    assert len(seen) == 1
    assert seen[0]["templates_root"] == install.PROJECT_ROOT


# ---------------------------------------------------------------------------
# ALREADY-DAMAGED
# ---------------------------------------------------------------------------


def test_a_registration_pointing_at_a_deleted_path_is_repaired_by_update(sandbox):
    home, _ = sandbox
    spec = bs.model_gateway_spec(
        os_key="Linux", exec_argv=["/gone/venv/bin/vct-model-gateway"],
    )
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    assert not Path("/gone/venv/bin/vct-model-gateway").exists()

    repaired = bs.model_gateway_spec(
        os_key="Linux", exec_argv=[sys.executable, "-m", "model_router"],
    )
    bs.rerender_if_registered(
        repaired, templates_root=REPO_ROOT, system="Linux", home=home,
    )
    body = bs.systemd_unit_path(spec, home).read_text(encoding="utf-8")
    assert "/gone/venv" not in body
    assert sys.executable in body


def test_a_hand_edited_unit_is_backed_up_before_it_is_rewritten(sandbox):
    """The user's bytes are never lost silently, even for a VCO-owned unit."""
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    unit = bs.systemd_unit_path(spec, home)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n# hand-edited by the user\n", encoding="utf-8")
    original = unit.read_text(encoding="utf-8")

    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    backups = list(unit.parent.glob(f"{unit.name}.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# UNINSTALL — the state scrub, both branches, every deletion
# ---------------------------------------------------------------------------


def _populate_state(state: Path, *, damaged: bool = False) -> dict:
    """Write the gateway's files AND the neighbours that must survive."""
    (state / "logs").mkdir(parents=True, exist_ok=True)
    (state / "model-gateway").mkdir(parents=True, exist_ok=True)
    written = {}
    names = list(bs.GATEWAY_STATE_FILES)
    if not damaged:
        names = [n for n in names if not n.endswith(".pre-vco")]
    for rel in names:
        path = state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {rel}\n", encoding="utf-8")
        written[rel] = path
    # Neighbours: other components' state and the user's own data.
    neighbours = {}
    for rel in ("hub.token", "hub.port", "services.toml", "launcher.db",
                "logs/vct-hub.log"):
        path = state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"NEIGHBOUR {rel}\n", encoding="utf-8")
        neighbours[rel] = (path, _sha(path))
    return {"written": written, "neighbours": neighbours}


def test_uninstall_removes_every_named_gateway_state_file(sandbox):
    """ACT."""
    _, state = sandbox
    fixture = _populate_state(state)
    audit = bs.remove_gateway_state(state)
    for rel, path in fixture["written"].items():
        assert not path.exists(), f"{rel} survived the scrub"
    assert all(not line.startswith("WARN") for line in audit), audit


def test_uninstall_leaves_every_neighbouring_file_byte_identical(sandbox):
    """LEAVE-ALONE, by hash. `~/.vct/` holds the hub's token and port, the
    services table and the launcher database — removing the directory (or
    walking it) would take the user's data with the gateway's."""
    _, state = sandbox
    fixture = _populate_state(state)
    bs.remove_gateway_state(state)
    for rel, (path, digest) in fixture["neighbours"].items():
        assert path.exists(), f"neighbour {rel} was removed"
        assert _sha(path) == digest, f"neighbour {rel} was modified"
    assert state.is_dir(), "the state root itself must never be removed"
    assert (state / "logs").is_dir(), "logs/ is shared with the hub"


def test_uninstall_on_a_machine_that_never_ran_the_gateway_removes_nothing(
    sandbox,
):
    """LEAVE-ALONE: the fresh axis of the uninstall. Reports, never warns."""
    _, state = sandbox
    (state / "hub.token").write_text("keep me\n", encoding="utf-8")
    before = _sha(state / "hub.token")
    audit = bs.remove_gateway_state(state)
    assert all("nothing to remove" in line for line in audit), audit
    assert _sha(state / "hub.token") == before


def test_uninstall_removes_the_pre_vco_backup_only_in_the_damaged_case(sandbox):
    """The already-damaged axis. `chat_model_context.json.pre-vco` exists
    only when something occupied the export path before VCO first wrote it
    (`BackupPolicy::Once`), so ACT and LEAVE-ALONE are two machines."""
    _, state = sandbox
    fixture = _populate_state(state, damaged=True)
    pre_vco = fixture["written"]["model-gateway/chat_model_context.json.pre-vco"]
    assert pre_vco.exists()
    bs.remove_gateway_state(state)
    assert not pre_vco.exists()

    # Healthy machine: the name is reported as absent, not warned about.
    healthy = state / "healthy"
    healthy.mkdir()
    audit = bs.remove_gateway_state(healthy)
    assert any(".pre-vco (nothing to remove)" in line for line in audit)
    assert not any("WARN" in line for line in audit)


def test_uninstall_keeps_the_gateway_directory_when_a_stranger_is_inside(
    sandbox,
):
    """LEAVE-ALONE with a named reason: a file someone else put there is
    reported to the user rather than deleted or silently orphaned."""
    _, state = sandbox
    _populate_state(state)
    stranger = state / "model-gateway" / "notes-from-the-user.txt"
    stranger.write_text("do not delete\n", encoding="utf-8")
    before = _sha(stranger)

    audit = bs.remove_gateway_state(state)
    assert (state / "model-gateway").is_dir()
    assert _sha(stranger) == before
    assert any("notes-from-the-user.txt" in line for line in audit), audit


def test_uninstall_removes_the_gateway_directory_once_it_is_empty(sandbox):
    """ACT, the counterpart to the test above."""
    _, state = sandbox
    _populate_state(state)
    bs.remove_gateway_state(state)
    assert not (state / "model-gateway").exists()


def test_uninstall_dry_run_removes_nothing(sandbox):
    """LEAVE-ALONE: `--dry-run` is a promise the plan makes to the user."""
    _, state = sandbox
    fixture = _populate_state(state)
    audit = bs.remove_gateway_state(state, dry_run=True)
    for path in fixture["written"].values():
        assert path.exists()
    assert any(line.startswith("would remove") for line in audit)


def test_uninstall_reports_a_file_it_cannot_remove(sandbox, monkeypatch):
    """Soft-fail: an unremovable file is reported and the scrub continues."""
    _, state = sandbox
    _populate_state(state)
    real_unlink = Path.unlink

    def selective(self, *a, **k):
        if self.name == "model-gateway.token":
            raise PermissionError("locked by another process")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", selective)
    audit = bs.remove_gateway_state(state)
    assert any("WARN" in line and "model-gateway.token" in line for line in audit)
    assert not (state / "model-gateway.pid").exists(), "the scrub kept going"


def test_the_scrub_list_matches_the_paths_the_gateway_actually_uses(sandbox):
    """The list is only right if it names what the code writes. Read the
    paths from the daemon's own config module, not from a second list."""
    _, state = sandbox
    from model_router import config as gateway_config

    expected = {
        gateway_config.token_path(),
        gateway_config.pid_path(),
        gateway_config.port_path(),
        gateway_config.log_path(),
        gateway_config.export_path(),
    }
    scrubbed = set(bs.gateway_state_paths(state))
    assert expected <= scrubbed, (
        f"the gateway writes files the uninstall does not remove: "
        f"{expected - scrubbed}"
    )


def test_the_rust_export_writers_backup_suffix_is_in_the_scrub_list():
    """The `.pre-vco` name is chosen in Rust; if it changes there and not
    here, an already-damaged machine keeps an orphan forever."""
    source = (
        REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands"
        / "chat_model_context.rs"
    ).read_text(encoding="utf-8")
    assert 'BackupPolicy::Once { ext: "pre-vco" }' in source
    assert any(name.endswith(".pre-vco") for name in bs.GATEWAY_STATE_FILES)


def test_uninstall_unregisters_the_gateway_and_says_so_when_there_was_none(
    sandbox, monkeypatch,
):
    """ACT and LEAVE-ALONE for the boot artefact half of uninstall."""
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])

    audit = bs.unregister(spec, home=home, system="Linux",
                          runner=lambda c, timeout=15: 0)
    assert any("nothing to remove" in line for line in audit)

    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    assert bs.systemd_unit_path(spec, home).exists()
    audit = bs.unregister(spec, home=home, system="Linux",
                          runner=lambda c, timeout=15: 0)
    assert not bs.systemd_unit_path(spec, home).exists()
    assert any("removed systemd user unit" in line for line in audit)


def test_the_uninstall_plan_names_the_gateway_artefact_per_os():
    import install  # type: ignore

    assert bs.MODEL_GATEWAY_UNIT_NAME in install._gateway_boot_artefact("Linux")
    assert bs.MODEL_GATEWAY_PLIST_LABEL in install._gateway_boot_artefact("Darwin")
    assert bs.MODEL_GATEWAY_TASK_NAME in install._gateway_boot_artefact("Windows")
    assert "Haiku" in install._gateway_boot_artefact("Haiku")


# ---------------------------------------------------------------------------
# CLI — the contract shared with `vct-hub`
# ---------------------------------------------------------------------------


def test_boot_status_prints_not_installed_and_exits_2(sandbox):
    out = io.StringIO()
    code = bs.run_boot_status(
        bs.model_gateway_spec(os_key="Linux"), stream=out,
    )
    assert out.getvalue().strip() == "not-installed"
    assert code == 2


def test_boot_status_prints_disabled_and_exits_1(sandbox):
    home, _ = sandbox
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    bs.register(spec, templates_root=REPO_ROOT, system="Linux", home=home)
    out = io.StringIO()
    # `which` returns None in this sandbox, so systemctl cannot confirm
    # "enabled" — the unit exists but nothing proves it is active.
    code = bs.run_boot_status(spec, stream=out)
    assert out.getvalue().strip() == "disabled"
    assert code == 1


def test_boot_status_reports_an_inspection_error_as_its_own_state(
    sandbox, monkeypatch,
):
    """Exit 3, not "disabled": "we could not look" and "we looked and it is
    off" are different answers, and collapsing them is the tri-state defect
    this release exists to end."""
    monkeypatch.setattr(
        bs, "status",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no user bus")),
    )
    out = io.StringIO()
    code = bs.run_boot_status(bs.model_gateway_spec(os_key="Linux"), stream=out)
    assert out.getvalue().startswith("error: ")
    assert code == 3


def test_register_boot_refuses_while_the_kill_switch_is_set(sandbox, monkeypatch):
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    err = io.StringIO()
    code = bs.run_register_boot(
        bs.model_gateway_spec(os_key="Linux"),
        templates_root=REPO_ROOT, stream=err,
    )
    assert code == 1
    assert "VCT_DISABLE_BOOT_SERVICE=1" in err.getvalue()


def test_unregister_boot_is_idempotent_on_a_machine_with_no_registration(
    sandbox,
):
    err = io.StringIO()
    code = bs.run_unregister_boot(
        bs.model_gateway_spec(os_key="Linux"), stream=err,
    )
    assert code == 0, err.getvalue()


def test_the_cli_exposes_all_three_flags_and_routes_them_to_the_shared_home(
    monkeypatch, sandbox,
):
    from model_router import __main__ as cli

    calls = []

    def _recorder(name):
        def run(spec, **kwargs):
            calls.append(name)
            return 0
        return run

    for name in ("run_register_boot", "run_unregister_boot", "run_boot_status"):
        monkeypatch.setattr(bs, name, _recorder(name))
    assert cli.main(["--register-boot"]) == 0
    assert cli.main(["--unregister-boot"]) == 0
    assert cli.main(["--boot-status"]) == 0
    assert calls == ["run_register_boot", "run_unregister_boot", "run_boot_status"]


def test_the_cli_holds_no_second_copy_of_the_registration_logic():
    """R16/tri-OS: the flags are argument parsing. A `systemctl` string in
    this file would mean a second mechanism."""
    code = _code_without_prose(
        REPO_ROOT / "claude_mcp_servers" / "model_router" / "__main__.py"
    )
    for forbidden in ("systemctl", "launchctl", "schtasks", "LaunchAgents",
                      ".service", ".plist"):
        assert forbidden not in code, (
            f"{forbidden!r} in model_router/__main__.py — boot logic belongs "
            "in vco_lib/boot_service.py"
        )


def test_the_gateway_exec_resolution_prefers_the_console_script(tmp_path,
                                                                monkeypatch):
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake_python = bindir / "python"
    fake_python.write_text("", encoding="utf-8")
    script = bindir / "vct-model-gateway"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    assert bs.resolve_gateway_exec() == [str(script)]


def test_the_gateway_exec_resolution_falls_back_to_module_form(tmp_path,
                                                               monkeypatch):
    """`python -m model_router`, never `python -m claude_mcp_servers.
    model_router`: `claude_mcp_servers/` has no `__init__.py`, so the dotted
    form only ever resolved from the repository root."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    fake_python = bindir / "python"
    fake_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    argv = bs.resolve_gateway_exec()
    assert argv == [str(fake_python), "-m", "model_router"]
