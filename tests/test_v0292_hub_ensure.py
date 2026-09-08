# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 ruling **R20** — ONE ensure-hub mechanism, not a second (nor a third).

Before this change, "find the ``vct-hub`` binary and start it if it is not
running" had THREE hand-maintained homes:

* ``templates/hooks/session-start-ensure-hub.sh``  (``detect_arch`` +
  ``find_hub_binary`` + the ``nohup`` spawn),
* ``templates/hooks/session-start-ensure-hub.ps1`` (``Get-ArchDirName`` +
  ``Get-HubExeNames`` + ``Find-HubBinary`` + ``Start-Process``),
* ``launcher/src-tauri/src/hub_launcher.rs``       (``find_hub_binary`` +
  ``find_on_path`` + ``is_executable`` + ``hub_binary_name``).

R20 said there must be one. :mod:`vco_lib.hub_ensure` is it, and all three
call-sites now delegate to it — class A of the repo's A>B>C cross-language
rule (one Python implementation, invoked via ``python -m vco_lib.hub_ensure``
on paths that are user-action-triggered and ms-scale).

What is pinned here
-------------------
1. **The module exists and exposes the SSOT API** — the deliverable itself
   (``rg -n hub_ensure`` returned 0 hits before this change).
2. **The spawning branch, BOTH ways** — a live hub is LEFT ALONE (no spawn),
   a dead/absent one IS started. The repo requires both halves of any branch
   that gates a spawning action.
3. **Binary-not-found is LOUD** — non-zero exit, named reason, never a
   silent "well, skip it".
4. **The discovery chain and the hub runtime contract are UNCHANGED** —
   override → install-folder → PATH → ``~/.vct/bin``; lockfile
   ``<vct_root_dir>/hub.pid``; port/token file names + the 7700 default.
5. **No call-site kept a private copy** — the two hooks and the Rust module
   delegate, and the mirrored primitives are gone.

Red-proofed against the pre-fix source: with ``vco_lib/hub_ensure.py``
removed, every test below errors on import/collection; and the three
delegation checks are additionally driven against verbatim pre-fix copies of
the two hooks plus a pre-fix-shaped ``hub_launcher.rs`` (see
§6 — the SAME helpers must reject verbatim pre-fix excerpts and accept the
current files, so neither an always-pass nor an always-fail helper survives).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from vco_lib import hub_ensure
from vco_lib.hub_ensure import EnsureState

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SH = REPO_ROOT / "templates" / "hooks" / "session-start-ensure-hub.sh"
HOOK_PS1 = REPO_ROOT / "templates" / "hooks" / "session-start-ensure-hub.ps1"
RUST_HUB = REPO_ROOT / "launcher" / "src-tauri" / "src" / "hub_launcher.rs"


# ---------------------------------------------------------------------------
# Delegation checks, factored so the SAME assertions can be run against the
# pre-fix files (the red-proof) as against the current ones.
# ---------------------------------------------------------------------------


def check_sh_delegates(text: str) -> list[str]:
    """Return the reasons ``text`` is NOT a delegating .sh hook (empty = ok)."""
    problems: list[str] = []
    if "vco_lib.hub_ensure" not in text:
        problems.append("does not invoke `python -m vco_lib.hub_ensure`")
    for mirrored in ("find_hub_binary()", "detect_arch()", 'command -v vct-hub'):
        if mirrored in text:
            problems.append(f"still carries its own copy of {mirrored!r}")
    return problems


def check_ps1_delegates(text: str) -> list[str]:
    """Return the reasons ``text`` is NOT a delegating .ps1 hook (empty = ok)."""
    problems: list[str] = []
    if "vco_lib.hub_ensure" not in text:
        problems.append("does not invoke `python -m vco_lib.hub_ensure`")
    for mirrored in ("Find-HubBinary", "Get-ArchDirName", "Get-HubExeNames"):
        if mirrored in text:
            problems.append(f"still carries its own copy of {mirrored!r}")
    return problems


def check_rust_delegates(text: str) -> list[str]:
    """Return the reasons ``text`` is NOT a delegating hub_launcher.rs."""
    problems: list[str] = []
    if "vco_lib.hub_ensure" not in text:
        problems.append("does not invoke `python -m vco_lib.hub_ensure`")
    for mirrored in ("fn find_on_path", "fn hub_binary_name", "fn is_executable"):
        if mirrored in text:
            problems.append(f"still carries its own copy of {mirrored!r}")
    return problems


# ---------------------------------------------------------------------------
# 1. The deliverable exists.
# ---------------------------------------------------------------------------


def test_hub_ensure_is_the_one_home_and_exposes_the_ssot_api() -> None:
    """R20's deliverable. Pre-fix, `vco_lib/hub_ensure.py` did not exist."""
    assert (REPO_ROOT / "vco_lib" / "hub_ensure.py").is_file()
    for name in (
        "find_hub_binary",
        "ensure_running",
        "is_running",
        "hub_pid",
        "hub_pid_file",
        "hub_port_file",
        "hub_token_file",
        "dist_arch_dir",
        "hub_binary_names",
        "main",
    ):
        assert hasattr(hub_ensure, name), f"hub_ensure must export {name}()"


# ---------------------------------------------------------------------------
# 2. The spawning branch — BOTH halves.
# ---------------------------------------------------------------------------


@pytest.fixture()
def spy_spawn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, bool]]:
    """Record every ``_spawn`` call instead of executing one."""
    calls: list[tuple[Path, bool]] = []

    def fake_spawn(binary: Path, wait: bool):  # type: ignore[no-untyped-def]
        calls.append((binary, wait))
        return hub_ensure.EnsureResult(
            state=EnsureState.STARTED, binary=str(binary), reason="spawned"
        )

    monkeypatch.setattr(hub_ensure, "_spawn", fake_spawn)
    return calls


def _write_hub(tmp_path: Path, name: str = "vct-hub") -> Path:
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_hub_already_running_is_left_alone_and_nothing_is_spawned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spy_spawn: list
) -> None:
    """THE LEAVE-ALONE CASE. A live hub must never be re-spawned."""
    monkeypatch.setattr(hub_ensure, "hub_pid", lambda: 4321)
    monkeypatch.setattr(hub_ensure, "is_running", lambda: True)
    # A perfectly resolvable binary is available — the ONLY reason not to
    # spawn must be the liveness check.
    monkeypatch.setenv("VCT_HUB_BIN", str(_write_hub(tmp_path)))

    res = hub_ensure.ensure_running()

    assert res.state is EnsureState.ALREADY_RUNNING
    assert res.pid == 4321
    assert res.exit_code == 0
    assert spy_spawn == [], "a running hub must NOT be re-spawned"
    assert "4321" in res.reason


def test_hub_not_running_spawns_start_if_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spy_spawn: list
) -> None:
    """THE ACT CASE. No live hub → the resolved binary is started."""
    monkeypatch.setattr(hub_ensure, "hub_pid", lambda: None)
    monkeypatch.setattr(hub_ensure, "is_running", lambda: False)
    exe = _write_hub(tmp_path)
    monkeypatch.setenv("VCT_HUB_BIN", str(exe))

    res = hub_ensure.ensure_running()

    assert res.state is EnsureState.STARTED
    assert res.exit_code == 0
    assert spy_spawn == [(exe, False)], "must spawn the resolved binary, detached"


def test_stale_lockfile_counts_as_not_running_and_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spy_spawn: list
) -> None:
    """A lockfile whose owner is dead is the crash-recovery state, not a hub.

    Same reading as ``hub_status.rs::probe`` → ``Stale``: start a fresh one.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "hub.pid").write_text("999999\nidentity-line\n", encoding="utf-8")
    monkeypatch.setenv("VCT_STATE_DIR", str(state_dir))
    monkeypatch.setattr(
        "vco_lib.deferral_probes.pid_is_alive", lambda pid: False
    )
    exe = _write_hub(tmp_path)
    monkeypatch.setenv("VCT_HUB_BIN", str(exe))

    assert hub_ensure.hub_pid() == 999999
    assert hub_ensure.is_running() is False
    res = hub_ensure.ensure_running()
    assert res.state is EnsureState.STARTED
    assert spy_spawn == [(exe, False)]


def test_real_spawn_invokes_start_if_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The spawn's argv is the documented CLI, and it is detached by default."""
    seen: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            seen["argv"] = argv
            seen["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    exe = _write_hub(tmp_path)

    res = hub_ensure._spawn(exe, wait=False)

    assert res.state is EnsureState.STARTED
    assert seen["argv"] == [str(exe), "--start-if-not-running"]
    if sys.platform != "win32":
        assert seen["kwargs"]["start_new_session"] is True


# ---------------------------------------------------------------------------
# 3. Binary not found → loud, non-zero, NAMED.
# ---------------------------------------------------------------------------


def test_binary_not_found_is_loud_nonzero_and_names_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spy_spawn: list
) -> None:
    monkeypatch.setattr(hub_ensure, "hub_pid", lambda: None)
    monkeypatch.setattr(hub_ensure, "is_running", lambda: False)
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))

    res = hub_ensure.ensure_running(repo_root=tmp_path / "no-checkout")

    assert res.state is EnsureState.BINARY_NOT_FOUND
    assert res.exit_code == 3, "not-found must be NON-ZERO, not a silent skip"
    assert spy_spawn == []
    # NAMED: the reason says what was searched, not just "failed".
    for token in ("VCT_HUB_BIN", "launcher/dist/", "$PATH", ".vct/bin"):
        assert token in res.reason, f"reason must name {token}: {res.reason!r}"


def test_cli_reports_not_found_on_stderr_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The cross-language surface is loud too — hooks only forward stderr."""
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))

    rc = hub_ensure.main(
        ["resolve", "--json", "--repo-root", str(tmp_path / "no-checkout")]
    )
    out = capsys.readouterr()

    assert rc == 3
    assert json.loads(out.out)["state"] == "binary_not_found"
    assert "hub_ensure" in out.err and "not found" in out.err


def test_exit_codes_avoid_1_and_2() -> None:
    """1 = unhandled exception (broken `import vco_lib`), 2 = argparse usage.

    A caller must be able to tell "no hub on this machine" from "the resolver
    itself could not run" — the same convention :mod:`vco_lib.containers` uses.
    """
    codes = set(hub_ensure.ENSURE_EXIT_CODES.values())
    assert 1 not in codes and 2 not in codes
    assert hub_ensure.ENSURE_EXIT_CODES[EnsureState.BINARY_NOT_FOUND] == 3
    assert hub_ensure.ENSURE_EXIT_CODES[EnsureState.SPAWN_FAILED] == 4


def test_spawn_failure_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise OSError("Exec format error")

    monkeypatch.setattr(subprocess, "Popen", boom)
    exe = _write_hub(tmp_path)

    res = hub_ensure._spawn(exe, wait=False)

    assert res.state is EnsureState.SPAWN_FAILED
    assert res.exit_code == 4
    assert "Exec format error" in res.reason


# ---------------------------------------------------------------------------
# 4. The discovery chain + the hub runtime contract are UNCHANGED.
# ---------------------------------------------------------------------------


def test_explicit_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = _write_hub(tmp_path, "vct-hub-dev")
    monkeypatch.setenv("VCT_HUB_BIN", str(exe))
    assert hub_ensure.find_hub_binary(repo_root=tmp_path) == exe


def test_non_executable_override_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Matches all three prior copies: a bad override does not abort the chain."""
    monkeypatch.setenv("VCT_HUB_BIN", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))
    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "no-checkout") is None

    # …and a LATER step still wins once one exists.
    bin_dir = tmp_path / "no-home" / ".vct" / "bin"
    bin_dir.mkdir(parents=True)
    user_copy = _write_hub(bin_dir)
    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "no-checkout") == user_copy


def test_install_folder_copy_is_preferred_over_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v0.2.63: a stale `vct-hub` on PATH must not beat the install copy."""
    path_dir = tmp_path / "onpath"
    path_dir.mkdir()
    _write_hub(path_dir)
    dist = tmp_path / "checkout" / "launcher" / "dist" / hub_ensure.dist_arch_dir()
    dist.mkdir(parents=True)
    install_copy = _write_hub(dist)

    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(path_dir))

    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "checkout") == install_copy


def test_extra_dirs_are_probed_before_the_checkout_dist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launcher's own install folder is the highest-priority anchor.

    It is the one input Python cannot derive (only the launcher process knows
    where its own binary lives), so it arrives as `--extra-dir`.
    """
    dist = tmp_path / "checkout" / "launcher" / "dist" / hub_ensure.dist_arch_dir()
    dist.mkdir(parents=True)
    _write_hub(dist)
    launcher_dir = tmp_path / "install-folder"
    launcher_dir.mkdir()
    sibling = _write_hub(launcher_dir)

    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))

    found = hub_ensure.find_hub_binary(
        repo_root=tmp_path / "checkout", extra_dirs=[launcher_dir]
    )
    assert found == sibling


def test_arch_less_dist_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dist = tmp_path / "checkout" / "launcher" / "dist"
    dist.mkdir(parents=True)
    flat = _write_hub(dist)
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "checkout") == flat


def test_hub_binary_names_are_per_platform() -> None:
    """Inherits the contract of the DELETED Rust `hub_binary_name()`.

    That test (`hub_binary_name_picks_per_platform_extension`) asserted the
    single per-platform filename. It could not be "re-pointed" when the Rust
    primitive was removed, because there was no Rust primitive left to assert
    against — so its contract moves HERE, and is strengthened rather than
    weakened: the module probes an ordered LIST (Windows tries `.exe` first,
    POSIX the bare name first, and each still falls back to the other, which
    is what the `.ps1` hook's `Get-HubExeNames` always did and what the Rust
    singular could not express).
    """
    names = hub_ensure.hub_binary_names()
    assert set(names) == {"vct-hub", "vct-hub.exe"}
    if sys.platform == "win32":
        assert names[0] == "vct-hub.exe"
    else:
        assert names[0] == "vct-hub"


def test_windows_exe_name_is_resolvable_on_any_host(tmp_path: Path, monkeypatch) -> None:
    """The `.exe` fallback is real, not decorative: a `vct-hub.exe` in an
    install folder resolves even when the bare name is absent."""
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    anchor_dir = tmp_path / "install-folder"
    anchor_dir.mkdir()
    exe = _write_hub(anchor_dir, "vct-hub.exe")
    found = hub_ensure.find_hub_binary(
        repo_root=tmp_path / "no-checkout", extra_dirs=[anchor_dir]
    )
    assert found == exe


def test_launcher_anchor_asymmetry_is_explicit(tmp_path: Path, monkeypatch) -> None:
    """`probe_repo_dist=False` — the launcher must NOT gain the checkout's
    `launcher/dist/` as a new discovery source.

    The hooks anchor step 2 on the CHECKOUT; the launcher anchors it on its
    OWN binary's directory. Consolidating the chain must preserve that
    difference, not flatten it — a `cargo run` launcher that suddenly found
    the repo's dist hub would be a behaviour change smuggled in as a merge.
    """
    dist = tmp_path / "checkout" / "launcher" / "dist" / hub_ensure.dist_arch_dir()
    dist.mkdir(parents=True)
    _write_hub(dist)
    monkeypatch.delenv("VCT_HUB_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-home"))

    # Hooks (default): the checkout's dist IS step 2.
    assert hub_ensure.find_hub_binary(repo_root=tmp_path / "checkout") is not None
    # Launcher: same checkout, but the dist probe is off.
    assert (
        hub_ensure.find_hub_binary(
            repo_root=tmp_path / "checkout", probe_repo_dist=False
        )
        is None
    )


def test_rust_caller_suppresses_the_repo_dist_probe() -> None:
    """The asymmetry above is only preserved if the Rust caller asks for it."""
    text = RUST_HUB.read_text(encoding="utf-8")
    assert "--no-repo-dist" in text, (
        "hub_launcher.rs must pass --no-repo-dist, else delegation silently "
        "adds the checkout's launcher/dist/ to the launcher's chain"
    )


def test_resolve_needs_only_the_stdlib(tmp_path: Path) -> None:
    """`resolve` must import under a bare system python.

    The launcher calls this during boot and during the UPDATE flow, when the
    orchestrator venv is precisely the thing in flux. So the import surface of
    the `resolve` path is stdlib + `vco_lib.paths` only; `pid_is_alive` (the
    one heavier import) is deliberately imported INSIDE `is_running()`.
    """
    source = (REPO_ROOT / "vco_lib" / "hub_ensure.py").read_text(encoding="utf-8")
    module_level = source.split("def hub_pid_file")[0]
    assert "from vco_lib.deferral_probes import" not in module_level
    assert "from vco_lib.paths import vct_root_dir" in module_level

    # The claim above is about the whole import SURFACE, not one forbidden
    # name, so scan it: nothing third-party at this module's top, and nothing
    # third-party at the top of the vco_lib modules it pulls in — otherwise
    # the property is inherited away one hop down. v0.2.94 added
    # `vco_lib.intfile` (the shared small-state-file reader) here; this is
    # what keeps that from being the hop that breaks the boot path.
    assert not _third_party_imports(module_level), _third_party_imports(module_level)

    imported = set(re.findall(r"^from (vco_lib\.\w+) import", module_level, re.M))
    assert imported, "the scan found no vco_lib import — it has gone blind"
    for name in sorted(imported):
        rel = Path(name.replace(".", "/") + ".py")
        dependency = (REPO_ROOT / rel).read_text(encoding="utf-8")
        head = dependency.split("\ndef ")[0]
        assert not _third_party_imports(head), f"{name}: {_third_party_imports(head)}"


#: Modules a bare system python has. Anything else in a boot-path dependency
#: means the UPDATE flow can fail to import before it can repair itself.
_STDLIB_ROOTS = {
    "argparse", "collections", "contextlib", "dataclasses", "enum", "errno",
    "functools", "hashlib", "io", "json", "logging", "os", "pathlib",
    "platform", "re", "shutil", "socket", "subprocess", "sys", "tempfile",
    "time", "typing", "urllib", "uuid",
}


def _third_party_imports(head: str) -> list[str]:
    """Import lines in ``head`` that a bare system python could not satisfy."""
    found = []
    for line in head.splitlines():
        if not line.startswith(("import ", "from ")):
            continue
        if line.startswith("from __future__"):
            continue
        root = line.split()[1].split(".")[0]
        if root == "vco_lib":
            continue  # followed transitively by the caller's own rule
        if root not in _STDLIB_ROOTS:
            found.append(line.strip())
    return found


def test_dist_arch_dir_matches_a_real_shipped_slot() -> None:
    """The slots that actually exist are linux-x64 / macos-arm64 / windows-x64."""
    arch = hub_ensure.dist_arch_dir()
    assert arch is not None
    known_prefixes = ("linux-", "macos-", "windows-")
    assert arch.startswith(known_prefixes), arch
    if sys.platform.startswith("linux"):
        assert arch in ("linux-x64", "linux-arm64"), arch


def test_hub_runtime_contract_paths_are_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """lockfile / port / token names + the 7700 default, under `vct_root_dir`."""
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    assert hub_ensure.hub_pid_file() == tmp_path / "hub.pid"
    assert hub_ensure.hub_port_file() == tmp_path / "hub.port"
    assert hub_ensure.hub_token_file() == tmp_path / "hub.token"
    assert hub_ensure.DEFAULT_HUB_PORT == 7700


@pytest.mark.parametrize(
    "content,expected",
    [
        ("7331\n", 7331),
        ("7331\nvct-hub 0.2.92\n", 7331),   # identity lines follow the pid
        ("  7331  \n", 7331),
        ("not-a-pid\n", None),
        ("", None),
        ("0\n", None),                      # 0 is never a startable owner
    ],
)
def test_hub_pid_reads_the_lockfile_like_hub_status_rs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str, expected
) -> None:
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path))
    (tmp_path / "hub.pid").write_text(content, encoding="utf-8")
    assert hub_ensure.hub_pid() == expected


def test_missing_lockfile_is_not_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / "absent"))
    assert hub_ensure.hub_pid() is None
    assert hub_ensure.is_running() is False


# ---------------------------------------------------------------------------
# 5. No call-site kept a private copy.
# ---------------------------------------------------------------------------


def test_sh_hook_delegates_to_the_module() -> None:
    assert check_sh_delegates(HOOK_SH.read_text(encoding="utf-8")) == []


def test_ps1_hook_delegates_to_the_module() -> None:
    assert check_ps1_delegates(HOOK_PS1.read_text(encoding="utf-8")) == []


def test_rust_launcher_delegates_to_the_module() -> None:
    assert check_rust_delegates(RUST_HUB.read_text(encoding="utf-8")) == []


def test_hooks_stay_in_lockstep() -> None:
    """CI enforces sibling parity; pin the specific things that must match."""
    sh = HOOK_SH.read_text(encoding="utf-8")
    ps1 = HOOK_PS1.read_text(encoding="utf-8")
    for token in ("vco_lib.hub_ensure", "ensure", "--repo-root", "--wait"):
        assert token in sh, f".sh missing {token!r}"
        assert token in ps1, f".ps1 missing {token!r}"
    # Both must keep the update gate LOCAL (it is deliberately not in the
    # module — the launcher parses the JSON deadline, the hooks use mtime).
    for text, name in ((sh, ".sh"), (ps1, ".ps1")):
        assert ".update-in-progress.json" in text, f"{name} lost the update gate"
    # Both must keep the never-block contract.
    assert "exit 0" in sh and "exit 0" in ps1


def test_ps1_keeps_its_bom() -> None:
    """OS-EXEMPT-PARITY note: the BOM is load-bearing for Windows PS 5.1."""
    assert HOOK_PS1.read_bytes()[:3] == b"\xef\xbb\xbf"


# ---------------------------------------------------------------------------
# 6. Red-proof, in-tree and hermetic: the delegation helpers must REJECT
#    pre-fix source and ACCEPT current source. Both halves, so neither an
#    always-pass nor an always-fail checker can satisfy §5.
# ---------------------------------------------------------------------------

#: Verbatim excerpts of the THREE pre-fix homes as they stood at c81f4fde.
#: Inlined rather than read from disk so the proof runs everywhere, always —
#: a skipped red-proof proves nothing.
PREFIX_SH = """
detect_arch() {
    local os arch
    os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
}
find_hub_binary() {
    if [ -n "${VCT_HUB_BIN:-}" ]; then printf '%s\\n' "$VCT_HUB_BIN"; fi
    on_path="$(command -v vct-hub 2>/dev/null || true)"
}
HUB_BIN="$(find_hub_binary || true)"
nohup "$HUB_BIN" --start-if-not-running >/dev/null 2>&1 &
"""

PREFIX_PS1 = """
function Get-ArchDirName { return "linux-x64" }
function Get-HubExeNames { return @("vct-hub", "vct-hub.exe") }
function Find-HubBinary {
    if ($env:VCT_HUB_BIN) { return $env:VCT_HUB_BIN }
    foreach ($name in Get-HubExeNames) { }
}
$HubBin = Find-HubBinary
Start-Process -FilePath $HubBin -ArgumentList "--start-if-not-running"
"""

PREFIX_RS = """
pub fn find_hub_binary() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("VCT_HUB_BIN") { }
    if let Some(on_path) = find_on_path("vct-hub") { return Some(on_path); }
    None
}
fn hub_binary_name() -> &'static str { "vct-hub" }
fn is_executable(path: &std::path::Path) -> bool { false }
fn find_on_path(name: &str) -> Option<PathBuf> { None }
"""


@pytest.mark.parametrize(
    "prefix_text,checker,marker",
    [
        (PREFIX_SH, check_sh_delegates, "find_hub_binary()"),
        (PREFIX_PS1, check_ps1_delegates, "Find-HubBinary"),
        (PREFIX_RS, check_rust_delegates, "fn find_on_path"),
    ],
    ids=["sh", "ps1", "rs"],
)
def test_delegation_checkers_reject_the_prefix_source(
    prefix_text: str, checker, marker: str
) -> None:
    """Without this, a checker that returned `[]` unconditionally would pass §5.

    Each pre-fix excerpt must be rejected for BOTH reasons: it does not call
    the module, and it still carries its own copy of the chain.
    """
    assert marker in prefix_text, "excerpt is not actually pre-fix shaped"
    problems = checker(prefix_text)
    assert problems, "pre-fix source must be REJECTED, got no problems"
    assert any("vco_lib.hub_ensure" in p for p in problems)
    assert any("still carries its own copy" in p for p in problems)


def test_delegation_checkers_accept_the_current_source() -> None:
    """The positive half — a checker cannot pass §5 by rejecting everything."""
    assert check_sh_delegates(HOOK_SH.read_text(encoding="utf-8")) == []
    assert check_ps1_delegates(HOOK_PS1.read_text(encoding="utf-8")) == []
    assert check_rust_delegates(RUST_HUB.read_text(encoding="utf-8")) == []
