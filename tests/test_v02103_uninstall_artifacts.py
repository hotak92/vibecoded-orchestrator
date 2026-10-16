# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Uninstall step [7] — what install places outside the clone (review
V02103-FINAL2 M-1). Every test runs against a temp HOME / LOCALAPPDATA /
VCT state root; nothing on the real machine is read or removed."""
from __future__ import annotations

import argparse
import importlib
import plistlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from vco_lib import uninstall_artifacts as ua  # noqa: E402


def _lnk(path: Path, target: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A .lnk stores its target path (ANSI in LinkInfo, UTF-16 in StringData).
    path.write_bytes(b"L\x00\x00\x00" + target.encode("utf-16-le") + b"\x00" * 8)
    return path


def _app(path: Path, bundle_id: str) -> Path:
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "MacOS" / "VCT Launcher").write_bytes(b"bin")
    with (path / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, fh)
    return path


def _pe_naming(exe: str) -> bytes:
    """A stand-in PE image that names itself, like every real VCO binary."""
    return b"MZ" + b"\0" * 64 + exe.rsplit(".", 1)[0].encode() + b"/src/main.rs\0"


@pytest.fixture
def win(tmp_path):
    home = tmp_path / "home"
    local = tmp_path / "LocalAppData"
    appdata = tmp_path / "Roaming"
    root = tmp_path / "vct"
    dest = local / "vct-launcher"
    dest.mkdir(parents=True)
    for exe in ua.WINDOWS_LAUNCHER_EXES:
        (dest / exe).write_bytes(_pe_naming(exe))
    (dest / "notes.txt").write_text("user's own file")  # foreign
    desk = _lnk(home / "Desktop" / "VCT Launcher.lnk", str(dest / "vct-launcher.exe"))
    start = _lnk(
        appdata / "Microsoft/Windows/Start Menu/Programs/VCT Launcher.lnk",
        str(dest / "vct-launcher.exe"),
    )
    other = _lnk(home / "Desktop" / "Other App.lnk", r"C:\Other\other.exe")
    (root / "state").mkdir(parents=True)
    (root / "state" / "engine-start.lock").write_text("")
    (root / "state" / "resync-claim.lock").write_text("")  # foreign to step 7
    env = {"LOCALAPPDATA": str(local), "APPDATA": str(appdata), "USERPROFILE": str(home)}
    plan = ua.build_plan(os_name="Windows", home=home, vct_root=root, env=env)
    return dict(plan=plan, dest=dest, desk=desk, start=start, other=other, root=root)


def test_dry_run_lists_every_windows_artefact_and_removes_nothing(win):
    plan = win["plan"]
    listed = {r.path for r in plan.remove}
    for exe in ua.WINDOWS_LAUNCHER_EXES:
        assert win["dest"] / exe in listed
    assert {win["desk"], win["start"], win["root"] / "state" / "engine-start.lock"} <= listed
    text = "\n".join(ua.plan_lines(plan))
    assert "vct-launcher.exe" in text and "VCT Launcher.lnk" in text
    audit = ua.execute(plan, dry_run=True)
    assert any(line.startswith("would remove") for line in audit)
    for p in listed:
        assert p.exists(), f"dry-run removed {p}"


def test_real_run_removes_them_and_leaves_foreign_files(win):
    plan = win["plan"]
    audit = ua.execute(plan, hub_running=lambda: False)
    for r in plan.remove:
        assert not r.path.exists(), r.path
    assert (win["dest"] / "notes.txt").read_text() == "user's own file"
    assert win["dest"].is_dir(), "a non-empty dir must survive"
    assert win["other"].exists(), "a user's other shortcut must survive"
    assert (win["root"] / "state" / "resync-claim.lock").exists()
    assert any(line.startswith("removed") for line in audit)


def test_a_same_named_lnk_that_is_not_ours_is_left(tmp_path):
    home = tmp_path / "home"
    foreign = _lnk(home / "Desktop" / "VCT Launcher.lnk", r"C:\Tools\something-else.exe")
    plan = ua.build_plan(os_name="Windows", home=home, vct_root=tmp_path / "vct",
                         env={"USERPROFILE": str(home)})
    assert foreign not in {r.path for r in plan.remove}


def test_a_running_hub_is_stopped_before_the_binaries_go(win):
    calls = []
    ua.execute(win["plan"], hub_running=lambda: True,
               runner=lambda cmd, timeout=15: calls.append(cmd) or 0)
    assert calls and calls[0][-1] == "--stop"
    assert calls[0][0].endswith("vct-hub.exe")


def test_a_locked_file_is_reported_and_the_step_continues(win, monkeypatch):
    real = Path.unlink

    def locked(self, *a, **k):
        if self.name == "vct-launcher.exe":
            raise PermissionError("in use")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    audit = ua.execute(win["plan"], hub_running=lambda: False)
    assert any("WARN" in a and "vct-launcher.exe" in a and "vct-hub --stop" in a for a in audit)
    assert not (win["dest"] / "vct-hub.exe").exists(), "kept going after the lock"


def test_linux_desktop_entries_and_flat_binary(tmp_path):
    home = tmp_path / "home"
    flat = home / ".local/share/vct-launcher/vct-launcher"
    flat.parent.mkdir(parents=True)
    flat.write_bytes(b"\x7fELF")
    body = "[Desktop Entry]\nType=Application\nName=VCT Launcher\nExec=\"x\"\n"
    menu = home / ".local/share/applications/vct-launcher.desktop"
    menu.parent.mkdir(parents=True)
    menu.write_text(body)
    desk = home / "Desktop/vct-launcher.desktop"
    desk.parent.mkdir(parents=True)
    desk.write_text("[Desktop Entry]\nName=My own thing\n")  # user's, same name
    plan = ua.build_plan(os_name="Linux", home=home, vct_root=tmp_path / "vct", env={})
    ua.execute(plan, hub_running=lambda: False)
    assert not flat.exists() and not flat.parent.exists()
    assert not menu.exists()
    assert desk.exists(), "a same-named entry that is not VCO's is left"


def test_macos_app_bundle_and_link_only_when_the_bundle_id_matches(tmp_path):
    home = tmp_path / "home"
    sysapps = tmp_path / "Applications"
    ours = _app(sysapps / "VCT Launcher.app", ua.LAUNCHER_BUNDLE_ID)
    (home / "Applications").mkdir(parents=True)
    link = home / "Applications" / "VCT Launcher.app"
    link.symlink_to(ours)
    plan = ua.build_plan(os_name="Darwin", home=home, vct_root=tmp_path / "vct",
                         env={}, system_applications=sysapps)
    kinds = {r.path: r.kind for r in plan.remove}
    assert kinds == {ours: "tree", link: "link"}
    ua.execute(plan, hub_running=lambda: False)
    assert not ours.exists() and not link.is_symlink()

    foreign = _app(sysapps / "VCT Launcher.app", "com.example.other")
    plan = ua.build_plan(os_name="Darwin", home=home, vct_root=tmp_path / "vct",
                         env={}, system_applications=sysapps)
    assert foreign not in {r.path for r in plan.remove}


def test_reviewer_backups_are_never_removed(tmp_path):
    root = tmp_path / "vct"
    backups = root / "backups" / "kg-reviewer-outside" / "20261016T000000Z"
    backups.mkdir(parents=True)
    (backups / "copy.md").write_text("user bytes")
    (root / "model-guidance.json").write_text("{}")
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "h", vct_root=root, env={})
    assert "LEFT IN PLACE" in "\n".join(ua.plan_lines(plan))
    audit = ua.execute(plan, hub_running=lambda: False)
    assert (backups / "copy.md").exists()
    assert not (root / "model-guidance.json").exists()
    assert any(a.startswith("left in place") for a in audit)


def test_new_state_files_this_cycle_are_the_paths_their_writers_use(tmp_path, monkeypatch):
    """Read the paths from the writers, not from a second list."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    from vco_lib import codegraph_walk, container_engine_start, model_selection, paths

    root = paths.vct_root_dir()
    written = [
        container_engine_start.engine_start_lock_path(),
        codegraph_walk.walk_claim_lock_path("Demo"),
        model_selection.state_record_path(),
        container_engine_start.engine_start_log_path(),
    ]
    for p in written:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "h", vct_root=root, env={})
    assert set(written) <= {r.path for r in plan.remove}


def _uninstall(monkeypatch, tmp_path, *, dry_run):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "vct"))
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(tmp_path / "vct" / "launcher.db"))
    install = importlib.import_module("install")
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(install._containers, "resolve", lambda *a, **k: argparse.Namespace(
        runtime=None, installed=None, compose=None, reason="test"))
    monkeypatch.setattr(install._boot_service, "unregister", lambda *a, **k: [])
    monkeypatch.setattr(install._boot_service, "unregister_hub_boot_service", lambda *a, **k: [])
    monkeypatch.setattr(ua, "_default_hub_running", lambda: False)
    lock = tmp_path / "vct" / "state" / "engine-start.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    args = argparse.Namespace(dry_run=dry_run, yes=True, quiet=True, keep_data=True,
                              remove_projects=False)
    assert install._run_uninstall(args) == 0
    return lock


def test_run_uninstall_dry_run_lists_step_7_and_keeps_the_lock(monkeypatch, tmp_path, capsys):
    lock = _uninstall(monkeypatch, tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert "[7]" in out and str(lock) in out
    assert lock.exists()


def test_run_uninstall_removes_the_engine_start_lock(monkeypatch, tmp_path):
    lock = _uninstall(monkeypatch, tmp_path, dry_run=False)
    assert not lock.exists()


# ── review R29 (N-1, N-2/N-4) ────────────────────────────────────────────


def test_a_user_exe_with_a_vco_name_is_left_in_place(tmp_path):
    """N-1: name alone is not ownership — a PE that does not name itself, or a
    non-PE file, under VCO's exe name is kept and reported."""
    local = tmp_path / "LocalAppData"
    dest = local / "vct-launcher"
    dest.mkdir(parents=True)
    (dest / "vct-hub.exe").write_bytes(b"MZ" + b"\0" * 64 + b"someone-elses-tool\0")
    (dest / "vct-updater.exe").write_bytes(b"not a PE at all, mentions vct-updater")
    (dest / "vct-launcher.exe").write_bytes(_pe_naming("vct-launcher.exe"))
    plan = ua.build_plan(os_name="Windows", home=tmp_path / "home", vct_root=tmp_path / "vct",
                         env={"LOCALAPPDATA": str(local)})
    removed = {r.path.name for r in plan.remove}
    kept = {p.name for p, _why in plan.keep}
    assert "vct-launcher.exe" in removed
    assert {"vct-hub.exe", "vct-updater.exe"} <= kept
    assert plan.hub_binaries == []
    ua.execute(plan, hub_running=lambda: False)
    assert (dest / "vct-hub.exe").exists() and (dest / "vct-updater.exe").exists()
    assert not (dest / "vct-launcher.exe").exists()


def test_a_reparse_point_launcher_dir_is_never_followed(tmp_path, monkeypatch):
    """N-2: a junction named vct-launcher (``is_symlink`` False on Windows) is
    treated like a symlink: nothing inside its target is planned."""
    local = tmp_path / "LocalAppData"
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "vct-hub.exe").write_bytes(_pe_naming("vct-hub.exe"))
    dest = local / "vct-launcher"
    dest.mkdir(parents=True)
    (dest / "vct-hub.exe").write_bytes(_pe_naming("vct-hub.exe"))
    monkeypatch.setattr(ua, "_is_reparse", lambda p: p == dest or p.is_symlink())
    plan = ua.build_plan(os_name="Windows", home=tmp_path / "home", vct_root=tmp_path / "vct",
                         env={"LOCALAPPDATA": str(local)})
    assert not any(r.path.parent == dest for r in plan.remove)
    assert dest not in plan.prune_if_empty


@pytest.mark.parametrize("redirected", ["state", "state/codegraph-walk-claims", "logs"])
def test_a_junction_under_the_state_dir_is_never_followed(tmp_path, monkeypatch, redirected):
    """R32 F1: a junction (a reparse point that is_symlink() misses) anywhere
    between the state root and a planned file stops that file being planned."""
    root = tmp_path / "vct"
    (root / "state" / "codegraph-walk-claims").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "state" / "engine-start.lock").write_text("")
    (root / "state" / "codegraph-walk-claims" / "a.lock").write_text("")
    (root / "logs" / "engine-start.log").write_text("")
    junction = root / redirected
    real = ua._is_reparse
    monkeypatch.setattr(ua, "_is_reparse", lambda p: p == junction or real(p))
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "home", vct_root=root, env={})
    assert not [r.path for r in plan.remove if junction in r.path.parents]


def test_a_junction_prune_candidate_is_never_rmdired(tmp_path, monkeypatch):
    """R32 F2: the prune loop consults the junction-aware check, not just
    is_symlink (a real dir standing in for a junction)."""
    junction = tmp_path / "looks-like-a-dir"
    junction.mkdir()
    attempted: list = []
    monkeypatch.setattr(type(junction), "rmdir", lambda self: attempted.append(self))
    real = ua._is_reparse
    monkeypatch.setattr(ua, "_is_reparse", lambda p: p == junction or real(p))
    plan = ua.Plan()
    plan.prune_if_empty.append(junction)
    ua.execute(plan, hub_running=lambda: False)
    assert attempted == []


def test_a_signature_straddling_the_scan_chunk_boundary_is_found(tmp_path):
    """R32 F3: the chunked scan's overlap carries a name split across reads."""
    exe = tmp_path / "vct-hub.exe"
    stem = b"vct-hub"
    exe.write_bytes(b"MZ" + b"\0" * ((1 << 20) - 2 - 3) + stem + b"\0" * 16)
    assert ua._is_vco_exe(exe)


def test_the_committed_windows_binaries_are_recognised():
    """R32 F3: the real release binaries in launcher/dist pass the check."""
    dist = Path(__file__).resolve().parents[1] / "launcher" / "dist" / "windows-x64"
    exes = [dist / name for name in ua.WINDOWS_LAUNCHER_EXES if (dist / name).is_file()]
    if not exes:
        pytest.skip("no committed Windows binaries in this checkout")
    assert all(ua._is_vco_exe(e) for e in exes)


def test_a_symlinked_prune_candidate_is_never_rmdired(tmp_path, monkeypatch):
    """N-4: the prune loop never even attempts rmdir on a link (on Windows an
    rmdir would remove a junction's reparse point; POSIX would just refuse,
    so the attempt itself is what this pins)."""
    real = tmp_path / "real-empty"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    attempted: list = []
    monkeypatch.setattr(type(link), "rmdir", lambda self: attempted.append(self))
    plan = ua.Plan()
    plan.prune_if_empty.append(link)
    ua.execute(plan, hub_running=lambda: False)
    assert attempted == []
    assert link.is_symlink() and real.is_dir()


def test_both_engine_start_logs_are_planned(tmp_path):
    """The starter's outcome log and the spawners' per-spawn stderr file
    (R31 M-1) are both removed, by the names container_engine_start owns."""
    from vco_lib import container_engine_start as ces

    root = tmp_path / "vct"
    (root / "logs").mkdir(parents=True)
    for name in (ces.ENGINE_START_LOG, ces.ENGINE_START_STDERR_LOG):
        (root / "logs" / name).write_text("x")
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "home", vct_root=root, env={})
    assert {r.path.name for r in plan.remove} >= {ces.ENGINE_START_LOG, ces.ENGINE_START_STDERR_LOG}


def test_shortcut_and_desktop_predicates_refuse_a_reparse_file(tmp_path, monkeypatch):
    """R35 G2: the content predicates never read through a link/junction."""
    lnk = _lnk(tmp_path / "VCT Launcher.lnk", r"C:\x\vct-launcher.exe")
    entry = tmp_path / "vct-launcher.desktop"
    entry.write_text(f"[Desktop Entry]\nName={ua.LAUNCHER_PRODUCT_NAME}\n")
    assert ua._is_vco_lnk(lnk) and ua._is_vco_desktop_entry(entry)
    real = ua._is_reparse
    monkeypatch.setattr(ua, "_is_reparse", lambda p: p in (lnk, entry) or real(p))
    assert not ua._is_vco_lnk(lnk)
    assert not ua._is_vco_desktop_entry(entry)


def test_a_non_ascii_exe_name_is_never_recognised(tmp_path):
    """R35 G2 / R32 F4: a stem that is not ASCII cannot be VCO's binary."""
    exe = tmp_path / "vct-hüb.exe"
    exe.write_bytes(b"MZ" + b"\0" * 64 + "vct-hüb".encode())
    assert ua._is_vco_exe(exe) is False


@pytest.mark.parametrize("redirected", ["logs", "state/codegraph-walk-claims"])
def test_a_vco_file_behind_a_junction_is_reported_left_not_dropped(tmp_path, monkeypatch, redirected):
    """FINAL3 L-1: a VCO file the step will not follow a link to is listed as
    left in place, and the plan never claims there is nothing to do."""
    root = tmp_path / "vct"
    (root / "state" / "codegraph-walk-claims").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "state" / "codegraph-walk-claims" / "a.lock").write_text("")
    (root / "logs" / "engine-start.log").write_text("")
    junction = root / redirected
    real = ua._is_reparse
    monkeypatch.setattr(ua, "_is_reparse", lambda p: p == junction or real(p))
    plan = ua.build_plan(os_name="Linux", home=tmp_path / "home", vct_root=root, env={})
    assert any(junction == p or junction in p.parents for p, _why in plan.keep)
    assert "[skip]" not in "\n".join(ua.plan_lines(plan))
