# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Starting the launcher with a session — tray only, once, and never rudely.

v0.2.95, ruling **R2** ("the launcher and the hub must auto-start when VS Code
starts"). Every gate in :mod:`vco_lib.launcher_ensure` gets an ACT test and a
LEAVE-ALONE test, because every one of them guards a side effect on the user's
desktop.

Nothing in this file starts a launcher, registers a boot unit, or touches
machine state: the process probe, the preference reader and the spawn are all
injected. The one thing that IS read for real is the committed
``launcher/dist/`` binary — and only to prove that the capability scan works
against a real Rust release build rather than against a synthetic file that
would prove nothing about the technique.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from vco_lib import launcher_ensure as le  # noqa: E402

LIB_RS = REPO_ROOT / "launcher" / "src-tauri" / "src" / "lib.rs"
AUTOSTART_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "src" / "commands" / "session_autostart.rs"
)
APP_STATE_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-launcher-core" / "src" / "db"
    / "app_state.rs"
)
AUTOSTART_TS = REPO_ROOT / "launcher" / "src" / "lib" / "session-autostart.ts"


class _Spawns(list):
    """Records what WOULD have been launched. Nothing is ever executed."""

    def __call__(self, argv, cwd):
        self.append((tuple(argv), cwd))


@pytest.fixture
def spawns():
    return _Spawns()


def _good_binary(tmp_path: Path) -> Path:
    """A stand-in launcher that carries the capability literal."""
    binary = tmp_path / "vct-launcher"
    binary.write_bytes(b"ELF\x00padding" + le.HIDDEN_START_FLAG.encode() + b"\x00more")
    binary.chmod(0o755)
    return binary


def _old_binary(tmp_path: Path) -> Path:
    """A pre-v0.2.95 launcher: no flag literal anywhere in it."""
    binary = tmp_path / "old" / "vct-launcher"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"ELF\x00--register-default-mcps\x00" + b"\xff" * 4096)
    binary.chmod(0o755)
    return binary


def _ensure(binary, spawns, *, repo_root=None, **over):
    """`ensure_running` with every probe pinned and the spawn recorded.

    ``repo_root`` defaults to a directory that does not exist, so a test never
    accidentally resolves the REAL committed ``launcher/dist/`` binary through
    step 2 of the discovery chain — which is what "no launcher on this
    machine" has to mean here.
    """
    kwargs: dict = {
        "scanner": lambda _name: None,
        "pref_reader": lambda _key: None,
        "env": {"DISPLAY": ":0"},
        "system": "Linux",
        "spawner": spawns,
        "repo_root": repo_root or Path("/nonexistent-checkout-for-tests"),
    }
    kwargs.update(over)
    if binary is not None:
        env: dict = dict(kwargs["env"])
        env[le.LAUNCHER_BIN_ENV] = str(binary)
        kwargs["env"] = env
    return le.ensure_running(**kwargs)


# ---------------------------------------------------------------------------
# The happy path — and the shape of what it starts
# ---------------------------------------------------------------------------


def test_an_absent_launcher_is_started_hidden(tmp_path, spawns, monkeypatch):
    binary = _good_binary(tmp_path)
    monkeypatch.setenv(le.LAUNCHER_BIN_ENV, str(binary))
    res = _ensure(binary, spawns)
    assert res.state is le.LauncherState.STARTED
    assert res.exit_code == 0
    assert len(spawns) == 1
    argv, _cwd = spawns[0]
    assert argv == (str(binary), le.HIDDEN_START_FLAG)


def test_the_started_launcher_never_inherits_the_callers_directory(
    tmp_path, spawns, monkeypatch
):
    """A GUI outlives the session that started it, and on Windows a process's
    cwd cannot be renamed or deleted — inheriting the user's project folder
    would lock it for as long as the launcher runs."""
    binary = _good_binary(tmp_path)
    monkeypatch.setenv(le.LAUNCHER_BIN_ENV, str(binary))
    project = tmp_path / "someones-project"
    project.mkdir()
    monkeypatch.chdir(project)
    _ensure(binary, spawns)
    _argv, cwd = spawns[0]
    assert Path(cwd) != project


def test_the_cwd_is_not_the_repo_root_the_hook_passes_either(tmp_path, spawns):
    """The hooks live at `<project>/.claude/hooks/` and pass `../..` as
    --repo-root — which IS the user's project. `repo_root` resolves the BINARY;
    it must never become the launcher's working directory."""
    binary = _good_binary(tmp_path)
    project = tmp_path / "a-real-project-dir"
    project.mkdir()
    _ensure(binary, spawns, repo_root=project)
    _argv, cwd = spawns[0]
    assert Path(cwd) != project
    # It IS the orchestrator checkout this module was imported from.
    assert Path(cwd) == Path(le.__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Every gate, both ways
# ---------------------------------------------------------------------------


def test_a_running_launcher_is_left_alone(tmp_path, spawns, monkeypatch):
    binary = _good_binary(tmp_path)
    monkeypatch.setenv(le.LAUNCHER_BIN_ENV, str(binary))
    res = _ensure(binary, spawns, scanner=lambda _name: 4242)
    assert res.state is le.LauncherState.RUNNING
    assert res.pid == 4242
    assert spawns == [], "a second launcher must never be spawned"


def test_a_running_launcher_short_circuits_before_reading_anything(tmp_path, spawns):
    """The common case must stay cheap: with a launcher up, the preference is
    not read and the binary is neither resolved nor scanned."""

    def _explode(_key):  # pragma: no cover — called only on regression
        raise AssertionError("the preference was read for a running launcher")

    res = _ensure(None, spawns, scanner=lambda _name: 7, pref_reader=_explode)
    assert res.state is le.LauncherState.RUNNING
    assert res.binary is None


def test_the_kill_switch_stops_the_leg_before_it_looks(tmp_path, spawns):
    binary = _good_binary(tmp_path)
    res = _ensure(
        binary, spawns,
        env={"DISPLAY": ":0", le.DISABLE_ENV: "1", le.LAUNCHER_BIN_ENV: str(binary)},
    )
    assert res.state is le.LauncherState.DISABLED_BY_ENV
    assert res.exit_code == 0
    assert spawns == []


def test_the_kill_switch_does_not_even_read_the_preference(tmp_path, spawns):
    def _explode(_key):  # pragma: no cover — called only on regression
        raise AssertionError("the env kill switch must not depend on the DB")

    res = _ensure(
        None, spawns, pref_reader=_explode, env={"DISPLAY": ":0", le.DISABLE_ENV: "1"},
    )
    assert res.state is le.LauncherState.DISABLED_BY_ENV


def test_a_user_who_turned_the_preference_off_gets_nothing(tmp_path, spawns):
    binary = _good_binary(tmp_path)
    res = _ensure(binary, spawns, pref_reader=lambda _key: "false")
    assert res.state is le.LauncherState.DISABLED_BY_PREF
    assert res.exit_code == 0
    assert spawns == []


def test_an_absent_preference_row_means_the_shipped_default_which_is_on(
    tmp_path, spawns
):
    """The delivery story for every existing install: `--update` writes no row,
    and no row means ON."""
    binary = _good_binary(tmp_path)
    res = _ensure(binary, spawns, pref_reader=lambda _key: None)
    assert res.state is le.LauncherState.STARTED
    assert le.DEFAULT_SESSION_AUTOSTART is True


def test_an_unreadable_preference_store_is_not_an_opt_out(tmp_path, spawns):
    """A fresh install has no launcher.db at all. Falling back to OFF there
    would make the shipped behaviour depend on a database existing."""

    def _raises(_key):
        raise sqlite_error()

    def sqlite_error():
        return RuntimeError("database is locked")

    binary = _good_binary(tmp_path)
    res = _ensure(binary, spawns, pref_reader=_raises)
    assert res.state is le.LauncherState.STARTED


def test_a_linux_session_with_no_desktop_is_skipped(tmp_path, spawns):
    """Claude Code over SSH, a container, CI: starting a GUI there is a
    guaranteed failure, not an attempt."""
    binary = _good_binary(tmp_path)
    res = _ensure(
        binary, spawns, env={"VCT_LAUNCHER_BIN": str(binary)}, system="Linux",
    )
    assert res.state is le.LauncherState.NO_DISPLAY
    assert res.exit_code == 0
    assert spawns == []


@pytest.mark.parametrize("var", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_either_display_variable_is_enough(var, tmp_path, spawns):
    binary = _good_binary(tmp_path)
    res = _ensure(
        binary, spawns, env={var: ":0", "VCT_LAUNCHER_BIN": str(binary)},
    )
    assert res.state is le.LauncherState.STARTED


@pytest.mark.parametrize("system", ["Darwin", "Windows"])
def test_macos_and_windows_are_never_declared_headless(system):
    """Neither has an honest cross-version probe, and guessing "headless" there
    would silently disable the feature for every user of those platforms."""
    assert le.display_available(env={}, system=system) is True


def test_a_machine_with_no_launcher_binary_is_a_silent_success(tmp_path, spawns):
    """A fresh clone, CI, a --no-launcher install: exit 0, nothing spawned, and
    no error — this machine simply has no GUI to start."""
    res = _ensure(None, spawns, env={"DISPLAY": ":0"})
    assert res.state is le.LauncherState.BINARY_NOT_FOUND
    assert res.exit_code == 0
    assert spawns == []


def test_a_binary_that_predates_the_flag_is_not_started(tmp_path, spawns):
    """The state a bundle update can produce: new hook, old binary. Starting it
    would open a window and take focus — the exact outcome this exists to
    prevent — so it is left alone, loudly."""
    binary = _old_binary(tmp_path)
    res = _ensure(binary, spawns)
    assert res.state is le.LauncherState.BINARY_TOO_OLD
    assert res.exit_code == 3, "the one state the user must act on"
    assert spawns == []
    assert "install.py --update" in res.reason


def test_a_spawn_failure_is_named_not_swallowed(tmp_path):
    def _boom(_argv, _cwd):
        raise OSError(13, "Permission denied")

    binary = _good_binary(tmp_path)
    res = _ensure(binary, None, spawner=_boom)
    assert res.state is le.LauncherState.SPAWN_FAILED
    assert res.exit_code == 4
    assert "Permission denied" in res.reason


def test_every_state_has_an_exit_code():
    for state in le.LauncherState:
        assert state in le.ENSURE_EXIT_CODES, state
    # 1 and 2 belong to an unhandled exception and to argparse (the reason
    # hub_ensure documents), so no state may claim them.
    assert not {1, 2} & set(le.ENSURE_EXIT_CODES.values())


# ---------------------------------------------------------------------------
# status reports; it never acts
# ---------------------------------------------------------------------------


def test_status_starts_nothing_and_says_what_ensure_would_do(tmp_path, monkeypatch):
    binary = _good_binary(tmp_path)
    monkeypatch.setattr(le, "_spawn", _must_not_spawn)
    res = le.launcher_status(
        scanner=lambda _name: None,
        pref_reader=lambda _key: None,
        env={"DISPLAY": ":0", le.LAUNCHER_BIN_ENV: str(binary)},
        system="Linux",
        repo_root=Path("/nonexistent-checkout-for-tests"),
    )
    assert res.state is le.LauncherState.NOT_RUNNING
    assert res.argv == (str(binary), le.HIDDEN_START_FLAG)


def test_status_answers_running_even_when_the_preference_is_off(tmp_path):
    """"Is the launcher up?" has one true answer, and a preference does not
    change it — a status that lied here would send someone hunting a process
    that is right in front of them."""
    res = le.launcher_status(
        scanner=lambda _name: 99,
        pref_reader=lambda _key: "false",
        env={},
        system="Linux",
    )
    assert res.state is le.LauncherState.RUNNING


def _must_not_spawn(*_a, **_k):  # pragma: no cover — regression guard
    raise AssertionError("status must never spawn")


# ---------------------------------------------------------------------------
# The capability scan — proven against a REAL Rust binary
# ---------------------------------------------------------------------------


def test_the_scan_finds_a_literal_split_across_read_boundaries(tmp_path):
    """The overlap between chunks is what makes a streamed scan sound; without
    it a flag straddling a 1 MiB boundary reads as absent and every launcher
    looks too old."""
    needle = le.HIDDEN_START_FLAG.encode()
    binary = tmp_path / "split"
    binary.write_bytes(b"\x00" * 10 + needle + b"\x00" * 10)
    assert le.binary_supports_hidden_start(binary, chunk_size=12) is True


def test_the_scan_says_no_for_an_absent_literal(tmp_path):
    binary = tmp_path / "plain"
    binary.write_bytes(b"\x00" * 4096)
    assert le.binary_supports_hidden_start(binary) is False


def test_an_unreadable_file_cannot_be_confirmed_so_it_is_a_no(tmp_path):
    assert le.binary_supports_hidden_start(tmp_path / "nope") is False


def test_the_scan_technique_works_on_the_real_committed_launcher():
    """The claim under test is "a Rust release build stores its flag literals
    verbatim". Asserting that against a synthetic file would prove nothing, so
    it is asserted against the committed launcher binary, using a flag that
    binary is known to implement."""
    dist = REPO_ROOT / "launcher" / "dist"
    candidates = sorted(dist.glob("*/vct-launcher")) + sorted(
        dist.glob("*/vct-launcher.exe")
    )
    if not candidates:
        pytest.skip("no committed launcher binary in launcher/dist/")
    binary = candidates[0]
    assert _scan_for(binary, b"--register-default-mcps") is True
    assert _scan_for(binary, b"--a-flag-no-launcher-has-ever-carried") is False


def _scan_for(path: Path, needle: bytes) -> bool:
    """`binary_supports_hidden_start` with a caller-chosen needle."""
    with open(path, "rb") as handle:
        tail = b""
        while True:
            block = handle.read(1 << 20)
            if not block:
                return False
            if needle in tail + block:
                return True
            tail = block[-(len(needle) - 1):]


# ---------------------------------------------------------------------------
# Cross-language pins — the reader and the writer must agree
# ---------------------------------------------------------------------------


# A cross-language constant is a tier-C mirror: the VALUE is the subject, so
# the pin has to read the other language's source text. What it must NOT pin
# is that language's DECLARATION SYNTAX — `&str` → `&'static str`,
# `pub` → `pub(crate)`, a line reflowed by rustfmt are all no-behaviour-change
# edits, and a test that goes red on them teaches the next editor that the
# ratchet is noise (v0.2.95 review MINOR-6). So: find the constant by NAME,
# skip whatever type annotation is spelled between `:` and `=`, and compare
# the literal. The annotation class excludes `= ; { } " /` so the lazy match
# can neither run past the end of a declaration nor bridge a doc comment that
# merely MENTIONS the name to some unrelated assignment further down the file.
_DECL = r"\b{name}\s*(?::\s*[^=;{{}}\"/]*?)?=\s*"


def _rust_str_const(src: str, name: str) -> str:
    """The string literal assigned to Rust/TS constant *name*."""
    match = re.search(_DECL.format(name=re.escape(name)) + r'"([^"]*)"', src)
    assert match, f"no declaration of {name} found — was it renamed or deleted?"
    return match.group(1)


def _rust_bool_const(src: str, name: str) -> bool:
    """The bool literal assigned to Rust/TS constant *name*."""
    match = re.search(_DECL.format(name=re.escape(name)) + r"(true|false)\b", src)
    assert match, f"no declaration of {name} found — was it renamed or deleted?"
    return match.group(1) == "true"


def test_the_flag_literal_matches_rust():
    src = LIB_RS.read_text(encoding="utf-8")
    assert _rust_str_const(src, "HIDDEN_START_FLAG") == le.HIDDEN_START_FLAG, (
        "the launcher's flag and the ensure's flag must be the same bytes — "
        "the ensure also SEARCHES the compiled binary for them"
    )


def test_the_pref_key_matches_rust():
    src = AUTOSTART_RS.read_text(encoding="utf-8")
    assert (
        _rust_str_const(src, "APP_STATE_SESSION_AUTOSTART")
        == le.APP_STATE_SESSION_AUTOSTART
    )


def test_the_default_matches_rust_and_typescript():
    rust = AUTOSTART_RS.read_text(encoding="utf-8")
    assert _rust_bool_const(rust, "DEFAULT_SESSION_AUTOSTART") is (
        le.DEFAULT_SESSION_AUTOSTART
    )
    ts = AUTOSTART_TS.read_text(encoding="utf-8")
    assert _rust_bool_const(ts, "DEFAULT_SESSION_AUTOSTART") is (
        le.DEFAULT_SESSION_AUTOSTART
    )


def test_the_value_pins_still_bite():
    """Positive control for the three tests above.

    Loosening a needle until nothing can fail it is the failure mode these
    helpers risk, so prove the opposite on synthetic sources: a REFLOWED
    declaration still resolves, and a CHANGED value is still caught.
    """
    for spelling in (
        'pub(crate) const HIDDEN_START_FLAG: &str = "--start-hidden";',
        "pub const HIDDEN_START_FLAG: &'static str = \"--start-hidden\";",
        'const HIDDEN_START_FLAG\n    : &str\n    = "--start-hidden";',
        'static HIDDEN_START_FLAG: &str="--start-hidden";',
    ):
        assert _rust_str_const(spelling, "HIDDEN_START_FLAG") == "--start-hidden"
    assert (
        _rust_str_const(
            'pub const HIDDEN_START_FLAG: &str = "--start-visible";',
            "HIDDEN_START_FLAG",
        )
        != le.HIDDEN_START_FLAG
    )
    assert _rust_bool_const("pub const D: bool = false;", "D") is False
    assert _rust_bool_const("export const D = true;", "D") is True


def test_the_truthiness_rule_matches_the_rust_reader():
    """The launcher WRITES this row and the hook READS it. A disagreement would
    show the user one thing and do another."""
    src = APP_STATE_RS.read_text(encoding="utf-8")
    flat = "".join(src.split())
    assert 'matches!(v.as_str(),"true"|"1")' in flat, (
        "app_state_get_bool's accepted set changed — update "
        "launcher_ensure.autostart_enabled to match"
    )
    for accepted in ("true", "1"):
        assert le.autostart_enabled(lambda _k, v=accepted: v) is True
    for rejected in ("false", "0", "TRUE", "yes", "", "  "):
        assert le.autostart_enabled(lambda _k, v=rejected: v) is False, rejected


def test_the_preference_default_survives_a_missing_row():
    assert le.autostart_enabled(lambda _k: None) is True


def test_the_toggle_has_a_real_consumer_end_to_end():
    """A toggle with no consumer is a shipped lie
    (``tests/test_v0291_pref_keys_have_consumers.py``). This one's consumer is
    unusual — a DIFFERENT PROCESS — so pin the whole chain: the page invokes
    the commands, the commands are registered with Tauri, and the key they
    write is the key the ensure reads."""
    page = (
        REPO_ROOT / "launcher" / "src" / "routes" / "preferences" / "+page.svelte"
    ).read_text(encoding="utf-8")
    for command in (
        "get_launcher_session_autostart",
        "set_launcher_session_autostart",
    ):
        assert f"'{command}'" in page, f"the page never invokes {command}"
        assert (
            f"commands::session_autostart::{command}"
            in LIB_RS.read_text(encoding="utf-8")
        ), f"{command} is not registered with Tauri — the invoke would fail"
    rust = AUTOSTART_RS.read_text(encoding="utf-8")
    assert "app_state_set_bool(APP_STATE_SESSION_AUTOSTART" in rust
    assert le.APP_STATE_SESSION_AUTOSTART in rust


# ---------------------------------------------------------------------------
# The shipped hooks call the module — on both OS flavours
# ---------------------------------------------------------------------------


def _hook_code(name: str) -> str:
    """A hook's EXECUTABLE lines — comments stripped, so a "must not contain"
    assertion never flags the comment that explains why."""
    text = (REPO_ROOT / "templates" / "hooks" / name).read_text(encoding="utf-8")
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _hook_invoked_modules(name: str) -> set:
    """Modules the hook actually runs with ``-m``.

    A plain "the module name appears in this file" assertion is not a wiring
    guard: it is satisfied by the error message that NAMES the module when the
    call fails. Mutating the invocation to a different module left exactly that
    assertion green, which is the source-scan trap this repo has a rule about.
    So parse the invocation instead — `-m <module>` in bash, the `"-m",
    "<module>"` argv pair in PowerShell.

    Executing the hook would be stronger still and is deliberately not done: it
    would start a real hub on the machine running the suite.
    """
    import re

    code = _hook_code(name)
    return set(re.findall(r'-m"?[,\s]+\s*"?([A-Za-z_][\w.]*)', code))


@pytest.mark.parametrize(
    "name", ["session-start-ensure-hub.sh", "session-start-ensure-hub.ps1"]
)
def test_the_shipped_hook_calls_this_module_and_decides_nothing_itself(name):
    """R20: one ensure mechanism. The hook CALLS the module; it must not carry
    its own copy of the probe, the preference read, or the flag."""
    assert "vco_lib.launcher_ensure" in _hook_invoked_modules(name), (
        f"{name} does not INVOKE the module (a mention in an error string is "
        "not wiring)"
    )
    code = _hook_code(name)
    for forbidden in ("pgrep", "tasklist", le.APP_STATE_SESSION_AUTOSTART):
        assert forbidden not in code, f"{forbidden!r} decided in {name}"


@pytest.mark.parametrize(
    "name", ["session-start-ensure-hub.sh", "session-start-ensure-hub.ps1"]
)
def test_the_hook_still_ensures_the_hub_and_the_gateway(name):
    """The leave-alone half of the hook edit: adding a third leg must not have
    displaced either of the two that were already there."""
    invoked = _hook_invoked_modules(name)
    assert {"vco_lib.hub_ensure", "vco_lib.gateway_ensure"} <= invoked, invoked


@pytest.mark.parametrize(
    "name", ["session-start-ensure-hub.sh", "session-start-ensure-hub.ps1"]
)
def test_the_hook_never_starts_a_visible_launcher(name):
    """Nothing in either hook may invoke the launcher binary directly: a bare
    invocation has no flag, so it would open a window and take focus."""
    code = _hook_code(name)
    assert "vct-launcher" not in code, (
        f"{name} names the launcher binary — the spawn belongs to "
        "vco_lib.launcher_ensure, which passes the tray-only flag"
    )


# ---------------------------------------------------------------------------
# The CLI contract the hooks consume
# ---------------------------------------------------------------------------


def test_the_shell_output_is_evaluable_and_names_the_state(capsys, monkeypatch):
    monkeypatch.setattr(le, "launcher_status", lambda **_k: le.LauncherEnsureResult(
        state=le.LauncherState.RUNNING, reason="it's up; don't touch it", pid=5,
    ))
    rc = le.main(["status", "--shell"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VCO_LAUNCHER_STATE=running" in out
    assert "VCO_LAUNCHER_PID=5" in out
    # Quoted for `eval`: the reason carries an apostrophe.
    assert "VCO_LAUNCHER_REASON=" in out
    env: dict = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    assert env["VCO_LAUNCHER_REASON"].startswith(("'", '"'))


def test_the_json_output_carries_what_a_caller_branches_on(capsys, monkeypatch):
    monkeypatch.setattr(le, "launcher_status", lambda **_k: le.LauncherEnsureResult(
        state=le.LauncherState.BINARY_TOO_OLD, reason="old", binary="/x/vct-launcher",
    ))
    rc = le.main(["status", "--json"])
    import json

    payload = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert payload["state"] == "binary_too_old"
    assert payload["running"] is False
    assert payload["binary"] == "/x/vct-launcher"


def test_the_module_runs_as_a_subprocess_the_hooks_can_call():
    """The hooks invoke `python -m vco_lib.launcher_ensure`; a module that only
    works when imported would fail exactly there.

    The environment is BUILT rather than inherited — a scrubbed `PATH` and the
    kill switch, so nothing here can put a tray icon on the developer's
    desktop — and then handed to `child_env` as its BASE, which is the
    supported way to keep a deliberate scrub AND the checkout's import pin.
    The pin is what makes the leg mean anything: `PYTHONPATH` alone loses to
    the `sys.path[0]` insert shipped code derives from an inherited
    `$VCT_ORCHESTRATOR_ROOT`, so an unpinned child can import a DIFFERENT
    checkout's `vco_lib.launcher_ensure` and green-light a module this tree
    does not have. `KG_BASE_DIR` is deliberately absent: with a caller-built
    `base` the helper leaves the root channels alone, and this child reaches
    no embedding backend.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "vco_lib.launcher_ensure", "status", "--json"],
        cwd=str(REPO_ROOT),
        env=child_env({"PATH": "/usr/bin:/bin"}, **{le.DISABLE_ENV: "1"}),
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    import json

    payload = json.loads(proc.stdout.decode())
    assert payload["state"] == "disabled_by_env"


# ---------------------------------------------------------------------------
# The extraction this lane made in hub_ensure stays honest
# ---------------------------------------------------------------------------


def test_the_launcher_reuses_the_one_discovery_chain(tmp_path, monkeypatch):
    """`find_dist_binary` is the hub's chain, parameterised — not a second
    copy. Prove the launcher walks it by planting a binary in step 2's
    arch-qualified dist slot."""
    from vco_lib import hub_ensure

    arch = hub_ensure.dist_arch_dir()
    if not arch:
        pytest.skip("no dist slot for this host")
    slot = tmp_path / "checkout" / "launcher" / "dist" / arch
    slot.mkdir(parents=True)
    planted = slot / le.launcher_binary_stem()
    planted.write_bytes(b"x")
    planted.chmod(0o755)
    monkeypatch.delenv(le.LAUNCHER_BIN_ENV, raising=False)
    found = le.find_launcher_binary(repo_root=tmp_path / "checkout")
    assert found == planted


def test_the_dist_slot_mapping_is_not_re_encoded_here():
    """Four hand-kept copies of the OS -> dist-subdir mapping already exist and
    are pinned by tests/test_launcher_dist_subdir_parity.py. A fifth in this
    module is exactly what that test exists to prevent."""
    src = (REPO_ROOT / "vco_lib" / "launcher_ensure.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines()
        if not line.lstrip().startswith("#") and ":: " not in line
    )
    for slot in ("linux-x64", "macos-arm64", "macos-x64", "windows-x64"):
        assert slot not in code, f"{slot!r} re-encoded in launcher_ensure.py"
