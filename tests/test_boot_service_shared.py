# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The shared boot-service home (v0.2.92 WP-10).

Three things are pinned here.

1. **The extraction is PURE.** ``install.py``'s three renderers moved into
   :mod:`vco_lib.boot_service` unchanged. Each parity test re-implements the
   HISTORICAL substitution set independently (spelled out below, not
   imported) and asserts the shared renderer reproduces it byte for byte —
   so the test would still catch a drift if someone changed both the
   renderer and its caller.

   The Windows renderer is the ONE deliberate exception, and it gets its own
   test rather than a relaxed assertion: the historical set omitted
   ``LOG_FILE`` while the shipped template sets
   ``VCT_STACK_LOG_FILE={{LOG_FILE}}``, so every Windows machine registered
   since v0.2.14 ran its logon task with the literal placeholder as a log
   path. The test asserts BOTH that the historical render carried the defect
   and that the new one does not.

2. **The Rust twin's contract has not drifted.** ``boot.rs`` is a declared
   class-C mirror: same verbs, same stdout words, same exit codes, no shared
   code. These tests read ``boot.rs``'s source, so a change on either side
   fails here.

3. **Every template placeholder has a substitution.** A ``{{KEY}}`` nobody
   fills is a promise the file makes to its reader and the renderer breaks —
   which is exactly how defect (1) survived three years.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import boot_service as bs  # noqa: E402

TEMPLATES = REPO_ROOT / "templates"
BOOT_RS = (
    REPO_ROOT / "launcher" / "src-tauri" / "vct-hub" / "src" / "boot.rs"
)


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Never touch the real user home or the real state root.

    Same prevention-not-recovery discipline as
    ``tests/test_materialize_boot_service.py``: the 2026-05-16 incident was
    a test that forgot one monkeypatch and rewrote the developer's actual
    systemd unit.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_state = tmp_path / "state"
    fake_state.mkdir()
    monkeypatch.setenv("VCT_USER_HOME_OVERRIDE", str(fake_home))
    monkeypatch.setenv("VCT_STATE_DIR", str(fake_state))
    monkeypatch.delenv("VCT_DISABLE_BOOT_SERVICE", raising=False)
    return fake_home


def _install_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    (root / "scripts").mkdir(parents=True)
    (root / "claude_mcp_servers").mkdir()
    (root / "scripts" / "launch-claude-mcp-stack.sh").write_text("#!/bin/bash\n")
    (root / "scripts" / "launch-claude-mcp-stack.ps1").write_text("# stub\n")
    (root / "state").mkdir()
    return root


def _naive_render(text: str, subs: dict) -> str:
    """The historical `_render_template`, re-implemented here on purpose."""
    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


# ---------------------------------------------------------------------------
# 1. Extraction purity
# ---------------------------------------------------------------------------


def test_linux_render_is_byte_identical_to_the_pre_extraction_output(
    tmp_path, _sandbox,
):
    install_path = _install_root(tmp_path)
    working_dir = install_path / "claude_mcp_servers"
    template = (
        TEMPLATES / "systemd" / "claude-mcp-containers.service.template"
    ).read_text(encoding="utf-8")

    # The historical substitution set, spelled out (install.py, v0.2.91).
    unit_path = (
        _sandbox / ".config" / "systemd" / "user" / "claude-mcp-containers.service"
    )
    log_file = _sandbox / ".local" / "state" / "vct" / "claude-mcp-containers.log"
    expected = _naive_render(template, {
        "INSTALLED_AT_PATH": str(unit_path),
        "WORKING_DIR": str(working_dir),
        "WRAPPER_SCRIPT": str(install_path / "scripts" / "launch-claude-mcp-stack.sh"),
        "LOG_FILE": str(log_file),
    })

    bs.register_linux(
        bs.container_stack_spec(install_path, working_dir, os_key="Linux"),
        template,
        home=_sandbox,
    )
    assert unit_path.read_text(encoding="utf-8") == expected


def test_macos_render_is_byte_identical_to_the_pre_extraction_output(
    tmp_path, _sandbox,
):
    install_path = _install_root(tmp_path)
    working_dir = install_path / "claude_mcp_servers"
    template = (
        TEMPLATES / "launchd"
        / "com.vibecodedtools.claude-mcp-containers.plist.template"
    ).read_text(encoding="utf-8")

    plist_path = (
        _sandbox / "Library" / "LaunchAgents"
        / "com.vibecodedtools.claude-mcp-containers.plist"
    )
    log_file = _sandbox / "Library" / "Logs" / "claude-mcp-containers.log"
    expected = _naive_render(template, {
        "INSTALLED_AT_PATH": str(plist_path),
        "LABEL": "com.vibecodedtools.claude-mcp-containers",
        "WORKING_DIR": str(working_dir),
        "WRAPPER_SCRIPT": str(install_path / "scripts" / "launch-claude-mcp-stack.sh"),
        "LOG_FILE": str(log_file),
    })

    bs.register_macos(
        bs.container_stack_spec(install_path, working_dir, os_key="Darwin"),
        template,
        home=_sandbox,
    )
    assert plist_path.read_text(encoding="utf-8") == expected


def test_windows_render_differs_from_pre_extraction_only_by_the_log_file_fix(
    tmp_path, monkeypatch, _sandbox,
):
    """The single deliberate behaviour change, both halves asserted.

    Half one: the pre-extraction substitution set really did leave
    ``{{LOG_FILE}}`` in the shipped XML — the defect is real, not inferred
    from reading the code. Half two: substituting it is the ONLY difference.
    """
    monkeypatch.setenv("USERDOMAIN", "TESTDOM")
    monkeypatch.setenv("USERNAME", "tester")
    install_path = _install_root(tmp_path)
    working_dir = install_path / "claude_mcp_servers"
    template = (
        TEMPLATES / "windows" / "claude-mcp-containers.task.xml.template"
    ).read_text(encoding="utf-8")

    spec = bs.container_stack_spec(install_path, working_dir, os_key="Windows")
    bs.register_windows(spec, template)
    rendered = spec.windows_task_xml_path.read_text(encoding="utf-8")

    # Reuse the render's own timestamp so the only difference under test is
    # the one this test is about.
    created_at = re.search(r"<Date>([^<]*)</Date>", rendered).group(1)
    historical = _naive_render(template, {
        "LABEL": "ClaudeMcpContainers",
        "WORKING_DIR": str(working_dir).replace("\\", "/"),
        "WRAPPER_SCRIPT": str(
            install_path / "scripts" / "launch-claude-mcp-stack.ps1"
        ).replace("\\", "/"),
        "CREATED_AT": created_at,
        "USER_ID": "TESTDOM\\tester",
    })
    assert "{{LOG_FILE}}" in historical, (
        "the pre-extraction renderer is supposed to have left this "
        "placeholder unsubstituted — if it did not, this test's premise is "
        "wrong and the 'fix' below is a regression"
    )

    log_file = str(bs.windows_log_file(spec)).replace("\\", "/")
    assert rendered == historical.replace("{{LOG_FILE}}", log_file)
    assert "{{" not in rendered


def test_the_windows_task_xml_still_parses_after_the_log_file_fix(
    tmp_path, monkeypatch, _sandbox,
):
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "martino")
    install_path = _install_root(tmp_path)
    spec = bs.container_stack_spec(
        install_path, install_path / "claude_mcp_servers", os_key="Windows",
    )
    bs.register_windows(
        spec,
        (TEMPLATES / "windows"
         / "claude-mcp-containers.task.xml.template").read_text(encoding="utf-8"),
    )
    ET.fromstring(spec.windows_task_xml_path.read_text(encoding="utf-8"))


def test_a_second_identical_register_writes_nothing(tmp_path, _sandbox):
    """LEAVE-ALONE: idempotence is what keeps `--update` from churning the
    unit (and its backup sidecars) on every run."""
    install_path = _install_root(tmp_path)
    working_dir = install_path / "claude_mcp_servers"
    template = (
        TEMPLATES / "systemd" / "claude-mcp-containers.service.template"
    ).read_text(encoding="utf-8")
    spec = bs.container_stack_spec(install_path, working_dir, os_key="Linux")

    bs.register_linux(spec, template, home=_sandbox)
    unit = bs.systemd_unit_path(spec, _sandbox)
    first = unit.stat().st_mtime_ns
    bs.register_linux(spec, template, home=_sandbox)
    assert unit.stat().st_mtime_ns == first
    assert not list(unit.parent.glob("*.bak-*")), "no backup for a no-op write"


def test_a_changed_render_backs_up_the_previous_unit(tmp_path, _sandbox):
    """ACT: the counterpart to the leave-alone above."""
    install_path = _install_root(tmp_path)
    template = (
        TEMPLATES / "systemd" / "claude-mcp-containers.service.template"
    ).read_text(encoding="utf-8")
    spec_a = bs.container_stack_spec(
        install_path, install_path / "claude_mcp_servers", os_key="Linux",
    )
    bs.register_linux(spec_a, template, home=_sandbox)
    spec_b = bs.container_stack_spec(
        install_path, tmp_path / "moved", os_key="Linux",
    )
    bs.register_linux(spec_b, template, home=_sandbox)
    unit = bs.systemd_unit_path(spec_b, _sandbox)
    assert str(tmp_path / "moved") in unit.read_text(encoding="utf-8")
    assert list(unit.parent.glob("*.bak-*")), "changed content must be backed up"


# ---------------------------------------------------------------------------
# 2. Placeholder coverage — the promise every template makes
# ---------------------------------------------------------------------------


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# What each renderer derives on its own, per OS.
_DERIVED = {
    "linux": {"INSTALLED_AT_PATH", "LOG_FILE", "BOOT_LOG_FILE"},
    "macos": {"INSTALLED_AT_PATH", "LABEL", "LOG_FILE", "BOOT_LOG_FILE"},
    "windows": {"LABEL", "CREATED_AT", "USER_ID", "LOG_FILE", "BOOT_LOG_FILE"},
}


@pytest.mark.parametrize(
    "relpath,os_key,arm",
    [
        ("systemd/claude-mcp-containers.service.template", "Linux", "linux"),
        (
            "launchd/com.vibecodedtools.claude-mcp-containers.plist.template",
            "Darwin", "macos",
        ),
        ("windows/claude-mcp-containers.task.xml.template", "Windows", "windows"),
    ],
)
def test_every_container_template_placeholder_has_a_substitution(
    relpath, os_key, arm, tmp_path,
):
    text = (TEMPLATES / relpath).read_text(encoding="utf-8")
    spec = bs.container_stack_spec(tmp_path, tmp_path, os_key=os_key)
    available = set(spec.substitutions) | _DERIVED[arm]
    missing = set(_PLACEHOLDER_RE.findall(text)) - available
    assert not missing, f"{relpath} has placeholders nothing substitutes: {missing}"


@pytest.mark.parametrize(
    "relpath,os_key,arm",
    [
        ("systemd/vct-model-gateway.service.template", "Linux", "linux"),
        (
            "launchd/com.vibecodedtools.vct-model-gateway.plist.template",
            "Darwin", "macos",
        ),
        ("windows/vct-model-gateway.task.xml.template", "Windows", "windows"),
    ],
)
def test_every_gateway_template_placeholder_has_a_substitution(
    relpath, os_key, arm,
):
    text = (TEMPLATES / relpath).read_text(encoding="utf-8")
    spec = bs.model_gateway_spec(os_key=os_key)
    available = set(spec.substitutions) | _DERIVED[arm]
    missing = set(_PLACEHOLDER_RE.findall(text)) - available
    assert not missing, f"{relpath} has placeholders nothing substitutes: {missing}"


def test_the_gateway_templates_exist_where_the_specs_look_for_them(tmp_path):
    """A spec that names a template the tree does not ship registers nothing
    and logs a skip — a silent no-op that looks like success."""
    for os_key, attr in (
        ("Linux", "template_linux"),
        ("Darwin", "template_macos"),
        ("Windows", "template_windows"),
    ):
        spec = bs.model_gateway_spec(os_key=os_key)
        relpath = getattr(spec, attr)
        assert bs.read_template(REPO_ROOT, relpath) is not None, relpath


# ---------------------------------------------------------------------------
# 3. Rust twin — the declared class-C mirror
# ---------------------------------------------------------------------------


def test_the_status_words_match_the_rust_twin():
    source = BOOT_RS.read_text(encoding="utf-8")
    for value in (s.value for s in bs.BootStatus):
        assert f'println!("{value}")' in source, (
            f"`{value}` is one of our contract words but boot.rs no longer "
            "prints it — the two CLIs have drifted"
        )
    assert 'println!("error: {}", e)' in source


def test_the_status_exit_codes_match_the_rust_twin():
    source = BOOT_RS.read_text(encoding="utf-8")
    for status_value, code in (
        (bs.BootStatus.ENABLED, 0),
        (bs.BootStatus.DISABLED, 1),
        (bs.BootStatus.NOT_INSTALLED, 2),
    ):
        block = source.split(f'println!("{status_value.value}")')[1]
        assert f"LifecycleResult::OkExit({code})" in block.split("\n")[1], (
            f"boot.rs maps {status_value.value} to a different exit code than "
            f"our {code}"
        )
        assert bs.BOOT_STATUS_EXIT_CODES[status_value] == code
    assert bs.BOOT_STATUS_ERROR_EXIT == 3


def test_the_three_verbs_exist_on_both_sides():
    source = BOOT_RS.read_text(encoding="utf-8")
    for verb in ("register_boot", "unregister_boot", "boot_status"):
        assert f"pub fn run_{verb}(" in source
        assert hasattr(bs, f"run_{verb}")


def test_posix_quoting_matches_the_rust_twins_three_cases():
    """The exact cases ``boot.rs``'s own unit tests pin — READ FROM
    ``boot.rs``, so a change on the Rust side fails here rather than
    silently making this a test of a stale expectation."""
    source = BOOT_RS.read_text(encoding="utf-8")
    assert 'shell_single_quote(&p), "\'/usr/local/bin/vct-hub\'"' in source
    assert 'shell_single_quote(&p), "\'/home/me/My Apps/vct-hub\'"' in source
    assert r"""assert_eq!(q, r"'/home/o'\''brien/vct-hub'");""" in source

    assert bs._posix_quote("/usr/local/bin/vct-hub") == "'/usr/local/bin/vct-hub'"
    assert bs._posix_quote("/home/me/My Apps/vct-hub") == "'/home/me/My Apps/vct-hub'"
    assert bs._posix_quote("/home/o'brien/vct-hub") == r"'/home/o'\''brien/vct-hub'"


def test_windows_status_parse_matches_the_rust_twins_none_on_localised_output():
    """LEAVE-ALONE, and the one that matters: a German ``Aktiviert`` must
    parse to None so the caller reaches its fallback. Guessing ``Enabled``
    here is the v0.2.54 G-8 defect."""
    assert bs.parse_windows_status_output("Status: Enabled") is bs.BootStatus.ENABLED
    assert bs.parse_windows_status_output("Status: Disabled") is bs.BootStatus.DISABLED
    assert bs.parse_windows_status_output("Status: Aktiviert") is None
    assert bs.parse_windows_status_output("") is None

    # And the Rust twin still returns None on the same input, rather than
    # having quietly reverted to the pre-G-8 "default to Enabled".
    source = BOOT_RS.read_text(encoding="utf-8")
    parser = source.split("fn parse_win_status_output(")[1].split("\n}")[0]
    assert parser.rstrip().endswith("None"), parser[-200:]


def test_register_enables_and_starts_on_both_sides():
    """The one behavioural promise our `--register-boot` docstring makes
    about the Rust twin: registration also enables AND starts."""
    source = BOOT_RS.read_text(encoding="utf-8")
    assert '"--user", "enable", "--now", "vct-hub.service"' in source
    gw = bs.model_gateway_spec(os_key="Linux")
    assert gw.enable_now is True
    # …and the container stack deliberately does NOT, because the install
    # has already brought the stack up.
    assert bs.container_stack_spec(
        REPO_ROOT, REPO_ROOT, os_key="Linux",
    ).enable_now is False


# ---------------------------------------------------------------------------
# 4. Shared behaviour
# ---------------------------------------------------------------------------


def test_the_disable_flag_gates_both_services(tmp_path, monkeypatch):
    """R16: a kill-switch honoured by one of two services is a flag nothing
    honours for the other."""
    monkeypatch.setenv("VCT_DISABLE_BOOT_SERVICE", "1")
    assert bs.boot_registration_disabled() is True
    for spec in (
        bs.container_stack_spec(tmp_path, tmp_path, os_key="Linux"),
        bs.model_gateway_spec(os_key="Linux"),
    ):
        assert bs.register(spec, templates_root=REPO_ROOT, system="Linux") is False


def test_the_disable_flag_is_off_by_default(tmp_path, _sandbox):
    """LEAVE-ALONE: without the env var, registration proceeds."""
    assert bs.boot_registration_disabled() is False
    install_path = _install_root(tmp_path)
    assert bs.register(
        bs.container_stack_spec(
            install_path, install_path / "claude_mcp_servers", os_key="Linux",
        ),
        templates_root=REPO_ROOT, system="Linux", home=_sandbox,
    ) is not None


def test_a_missing_template_is_a_skip_not_a_crash(tmp_path, _sandbox):
    spec = bs.container_stack_spec(tmp_path, tmp_path, os_key="Linux")
    events = []
    assert bs.register(
        spec, templates_root=tmp_path / "no-templates-here",
        on_event=lambda p, d, data=None: events.append((p, d)),
        system="Linux", home=_sandbox,
    ) is False
    assert any(p == "skip" for p, _ in events)


def test_read_template_resolves_against_the_given_root_not_the_cwd(
    tmp_path, monkeypatch,
):
    """Delivery check (4): no machine-shape assumption. The renderers must
    read the orchestrator's templates wherever the clone happens to be."""
    root = tmp_path / "some" / "odd" / "clone"
    (root / "templates" / "systemd").mkdir(parents=True)
    (root / "templates" / "systemd" / "x.template").write_text("hi", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert bs.read_template(root, "templates/systemd/x.template") == "hi"
    assert bs.read_template(tmp_path, "templates/systemd/x.template") is None


def test_the_boot_log_is_never_the_daemons_own_log(tmp_path):
    """If these were the same file every log record would be written twice —
    once by the daemon's file handler, once by the init system capturing its
    stderr."""
    log = tmp_path / "logs" / "model-gateway.log"
    assert bs.boot_log_file(log) != log
    assert bs.boot_log_file(log).parent == log.parent
    assert bs.boot_log_file(log).name == "model-gateway.boot.log"


def test_windows_substitutions_are_xml_escaped_once(tmp_path, monkeypatch):
    """R23, pre-existing: only USER_ID was escaped, so an install path with
    an ``&`` produced XML `schtasks /Create /XML` rejects outright."""
    monkeypatch.setenv("USERDOMAIN", "ACME&CO")
    monkeypatch.setenv("USERNAME", "alice")
    install_path = tmp_path / "C" / "Users" / "A&B" / "vco"
    (install_path / "scripts").mkdir(parents=True)
    (install_path / "scripts" / "launch-claude-mcp-stack.ps1").write_text("#", "utf-8")
    (install_path / "state").mkdir()
    spec = bs.container_stack_spec(install_path, install_path, os_key="Windows")
    bs.register_windows(
        spec,
        (TEMPLATES / "windows"
         / "claude-mcp-containers.task.xml.template").read_text(encoding="utf-8"),
    )
    rendered = spec.windows_task_xml_path.read_text(encoding="utf-8")
    root = ET.fromstring(rendered)  # would raise pre-fix
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:UserId", ns).text == "ACME&CO\\alice"
    assert root.find(".//t:WorkingDirectory", ns).text == str(
        install_path,
    ).replace("\\", "/")
    assert "&amp;amp;" not in rendered, "double-escaped: two escape points again"


def test_a_plain_windows_path_is_not_altered_by_the_escape(tmp_path, monkeypatch):
    """LEAVE-ALONE: escaping must be a no-op for paths without XML
    metacharacters, which is every path on a normal machine."""
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "martino")
    install_path = _install_root(tmp_path)
    spec = bs.container_stack_spec(install_path, install_path, os_key="Windows")
    template = (
        TEMPLATES / "windows" / "claude-mcp-containers.task.xml.template"
    ).read_text(encoding="utf-8")
    bs.register_windows(spec, template)
    rendered = spec.windows_task_xml_path.read_text(encoding="utf-8")
    assert str(install_path).replace("\\", "/") in rendered
    assert "WORKGROUP\\martino" in rendered


# ---------------------------------------------------------------------------
# 5. Unregister — ACT and LEAVE-ALONE on every OS
# ---------------------------------------------------------------------------


def _spec_for_unregister(tmp_path) -> bs.BootServiceSpec:
    return bs.container_stack_spec(tmp_path, tmp_path, os_key="Linux")


def test_linux_unregister_removes_a_present_unit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit = unit_dir / bs.CONTAINER_STACK_UNIT_NAME
    unit.write_text("[Unit]\n", encoding="utf-8")
    neighbour = unit_dir / "someone-elses.service"
    neighbour.write_text("[Unit]\nDescription=not ours\n", encoding="utf-8")
    before = neighbour.read_bytes()

    calls = []
    monkeypatch.setattr(bs.shutil, "which", lambda n: f"/usr/bin/{n}")
    audit = bs.unregister(
        _spec_for_unregister(tmp_path), home=home, system="Linux",
        runner=lambda cmd, timeout=15: calls.append(list(cmd)) or 0,
    )
    assert not unit.exists()
    assert any("removed systemd user unit" in line for line in audit)
    assert neighbour.read_bytes() == before, "a neighbouring unit was touched"


def test_linux_unregister_leaves_alone_when_nothing_is_registered(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    neighbour = unit_dir / "someone-elses.service"
    neighbour.write_text("[Unit]\n", encoding="utf-8")
    before = neighbour.read_bytes()

    monkeypatch.setattr(bs.shutil, "which", lambda n: f"/usr/bin/{n}")
    audit = bs.unregister(
        _spec_for_unregister(tmp_path), home=home, system="Linux",
        runner=lambda cmd, timeout=15: 0,
    )
    assert any("nothing to remove" in line for line in audit)
    assert neighbour.read_bytes() == before
    assert not any(line.startswith("WARN") for line in audit)


def test_macos_unregister_act_and_leave_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(bs.shutil, "which", lambda n: f"/bin/{n}")
    spec = _spec_for_unregister(tmp_path)

    # LEAVE-ALONE first — nothing registered.
    audit = bs.unregister(
        spec, home=home, system="Darwin", runner=lambda c, timeout=15: 0,
    )
    assert any("nothing to remove" in line for line in audit)

    # ACT — a plist exists.
    plist = agents / f"{bs.CONTAINER_STACK_PLIST_LABEL}.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    audit = bs.unregister(
        spec, home=home, system="Darwin", runner=lambda c, timeout=15: 0,
    )
    assert not plist.exists()
    assert any("removed LaunchAgent plist" in line for line in audit)


def test_windows_unregister_does_not_remove_a_task_xml_the_spec_omits(
    tmp_path, monkeypatch,
):
    """LEAVE-ALONE with a reason: the container stack's task XML lives inside
    the clone the user is deleting, so the legacy caller passes no path and
    nothing outside the clone is touched."""
    monkeypatch.setattr(bs.shutil, "which", lambda n: f"/w/{n}")
    spec = bs.container_stack_unregister_spec()
    assert spec.windows_task_xml_path is None
    audit = bs.unregister(
        spec, home=tmp_path, system="Windows", runner=lambda c, timeout=15: 0,
    )
    assert not any("task XML" in line for line in audit)


def test_unregister_honours_the_same_home_override_as_register(
    tmp_path, monkeypatch, _sandbox,
):
    """R23, pre-existing: PR-16's sandbox hardened the WRITE path only, so a
    caller that omitted ``home`` deleted from the REAL user home. Register
    and unregister must resolve the same home or the sandbox has a hole
    exactly where deletion happens."""
    spec = bs.model_gateway_spec(os_key="Linux", exec_argv=["/gw"])
    unit = bs.systemd_unit_path(spec, _sandbox)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(bs.shutil, "which", lambda n: None)

    # No `home=` — the override must still be what is used.
    audit = bs.unregister(spec, system="Linux", runner=lambda c, timeout=15: 0)
    assert not unit.exists()
    assert str(_sandbox) in " ".join(audit)


def test_unregister_never_raises_even_when_the_runner_explodes(
    tmp_path, monkeypatch,
):
    def boom(cmd, timeout=15):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(bs.shutil, "which", lambda n: f"/usr/bin/{n}")
    home = tmp_path / "home"
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    (home / ".config" / "systemd" / "user" / bs.CONTAINER_STACK_UNIT_NAME).write_text(
        "[Unit]\n", encoding="utf-8",
    )
    audit = bs.unregister(
        _spec_for_unregister(tmp_path), home=home, system="Linux", runner=boom,
    )
    assert any("WARN" in line for line in audit)


# ---------------------------------------------------------------------------
# 6. Status
# ---------------------------------------------------------------------------


def test_status_is_not_installed_when_no_artefact_exists(tmp_path, _sandbox):
    spec = bs.model_gateway_spec(os_key="Linux")
    assert bs.status(spec, home=_sandbox, system="Linux") is bs.BootStatus.NOT_INSTALLED


def test_status_is_disabled_when_the_unit_exists_but_systemctl_is_absent(
    tmp_path, monkeypatch, _sandbox,
):
    """OK requires evidence; the absence of a checker is not evidence."""
    spec = bs.model_gateway_spec(os_key="Linux")
    unit = bs.systemd_unit_path(spec, _sandbox)
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(bs.shutil, "which", lambda n: None)
    assert bs.status(spec, home=_sandbox, system="Linux") is bs.BootStatus.DISABLED


def test_status_on_an_unsupported_os_is_not_installed(_sandbox):
    spec = bs.model_gateway_spec(os_key="Linux")
    assert bs.status(spec, home=_sandbox, system="FreeBSD") is (
        bs.BootStatus.NOT_INSTALLED
    )


# ---------------------------------------------------------------------------
# 7. The cleanup shim is GONE — one home, no forwarder to drift
# ---------------------------------------------------------------------------


def _python_files_importing(module_dotted: str, files) -> list[str]:
    """Files whose IMPORT statements name ``module_dotted`` — by AST, so a
    docstring or comment that merely mentions the module (this file's own,
    for one) does not count. The register-34 lesson: a scanner that cannot
    tell "present" from "actually imported" is either a false red (prose)
    or a false green (a deleted symbol surviving in a comment)."""
    import ast
    head, _, leaf = module_dotted.rpartition(".")
    hits: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue  # `global`/`nonlocal` carry `names` too — as str
            names = [a.name for a in node.names]
            if isinstance(node, ast.Import) and module_dotted in names:
                hits.append(str(path.relative_to(REPO_ROOT)))
            elif isinstance(node, ast.ImportFrom) and (
                node.module == module_dotted
                or (node.module == head and leaf in names)
            ):
                hits.append(str(path.relative_to(REPO_ROOT)))
    return sorted(set(hits))


def _shipped_python_files():
    roots = [REPO_ROOT / "vco_lib", REPO_ROOT / "templates",
             REPO_ROOT / "claude_mcp_servers", REPO_ROOT / "tests"]
    for root in roots:
        for path in root.rglob("*.py"):
            if any(part in {"node_modules", "target", ".venv", "__pycache__"}
                   for part in path.parts):
                continue
            yield path
    # install.py is scanned explicitly (an rglob on REPO_ROOT would walk
    # node_modules / cargo targets for nothing).
    yield REPO_ROOT / "install.py"


def test_the_cleanup_shim_is_deleted_and_nothing_imports_it():
    """v0.2.92 duplication-merge (PLAN-EXTENSION §3.6): the one-release
    forwarding shim ``vco_lib/boot_service_cleanup.py`` promised its own
    deletion once its two importers were repointed. Both are: the uninstaller
    calls :func:`vco_lib.boot_service.unregister` with
    :func:`container_stack_unregister_spec`, and
    ``tests/test_uninstall_boot_service.py`` injects ``runner=``. This pins the
    absence so the shim cannot quietly come back as a third home.

    Two checks, deliberately separate: the FILE is gone, and no ``.py`` under
    the shipped trees / ``tests/`` / ``install.py`` IMPORTS it (AST — prose
    mentions are history, not call sites). Red-proofed three ways: a comment
    naming the module → PASS; a live ``from vco_lib import
    boot_service_cleanup`` → FAIL; the shim file restored → FAIL.
    """
    assert not (REPO_ROOT / "vco_lib" / "boot_service_cleanup.py").exists(), (
        "the forwarding shim is back; delete it and repoint the importer"
    )
    importers = _python_files_importing(
        "vco_lib.boot_service_cleanup", _shipped_python_files(),
    )
    assert not importers, f"still IMPORTED by: {importers}"


def test_container_stack_unregister_spec_names_match_the_register_spec(tmp_path):
    """The whole reason for one home: what install writes is what uninstall
    removes. The unregister spec must carry the SAME three names as the
    register spec, and omit ONLY the task-XML path (which lives inside the
    clone being deleted)."""
    reg = bs.container_stack_spec(tmp_path, tmp_path, os_key="Windows")
    unreg = bs.container_stack_unregister_spec()
    assert (reg.unit_name, reg.plist_label, reg.task_name) == (
        unreg.unit_name, unreg.plist_label, unreg.task_name
    )
    assert reg.windows_task_xml_path is not None
    assert unreg.windows_task_xml_path is None


def test_install_py_no_longer_holds_a_second_copy_of_the_renderers():
    """The straggler proof, as a test: the extraction is not done while the
    old bodies are still there to drift."""
    # Code shapes only: the module header's prose about systemd / launchd /
    # Task Scheduler is documentation of a concern install.py still calls
    # into, and deleting prose is not what "extracted" means.
    code = "\n".join(
        line for line in (REPO_ROOT / "install.py").read_text(
            encoding="utf-8",
        ).splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in (
        "def _render_template(",
        "def _backup_and_write_idempotent(",
        'template = _read_template("templates/systemd',
        "loginctl",
        '"bootstrap"',
        'os.environ.get("USERDOMAIN"',
        '"/Create", "/TN"',
    ):
        assert forbidden not in code, (
            f"install.py still contains {forbidden!r} — boot-service logic "
            "that belongs in vco_lib/boot_service.py"
        )


def test_the_disable_env_string_appears_in_exactly_one_module():
    """A second inline read of the kill-switch is how a flag comes to gate
    one of two services and silently not the other. install.py may NAME it
    in a log line — that is the user-facing message — but the decision has
    one home."""
    readers = []
    for path in (REPO_ROOT / "install.py", *(REPO_ROOT / "vco_lib").glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "VCT_DISABLE_BOOT_SERVICE" in line and "environ" in line:
                readers.append(path.name)
    assert readers == [], (
        "the env var is read inline in "
        f"{readers} — it must go through boot_service.boot_registration_disabled()"
    )
    assert bs.DISABLE_ENV == "VCT_DISABLE_BOOT_SERVICE"
