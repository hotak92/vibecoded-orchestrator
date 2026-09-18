# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A missing shipped `_lib/` helper must be LOUD, not a silent no-op.

v0.2.95 review MAJOR-1, then MAJOR-2. The write-side hooks source their
`_lib/` helpers conditionally and skip the work when one is absent — correct,
because a PostToolUse hook may never error on the user's Edit. What was wrong
is that the skip said NOTHING: after this cycle moved all routing into
`_lib/route-touched-path`, a project whose copy is missing (hand-copied hooks,
a half-applied bundle, an over-eager cleanup) syncs nothing at all on any
write, and neither the hook, the failure ledger (which only sees kg-sync runs
that HAPPENED) nor the SessionStart write-path probe (which tests the Python
import, not these files) had anything to say about it.

MAJOR-2 is why this file is parameterised rather than written three times.
The first fix closed the class for ONE of the three `_lib` files by hand, in
the two places in front of it; `bash-write-targets` still exited silently, a
missing `code-extensions` still routed no code file at all, and the
SessionStart probe printed "KG write routing: OK" while every CLI write was
being dropped — a probe that reports OK for a broken pipeline is worse than
no probe. So the SET now has one home, `vco_required_hook_libs` in
`_lib/emit-context.sh`, and this file READS it (by running it, not by
scanning source) and drives every row. A fourth lib added there is driven
automatically; a fourth lib with no driver here fails
`test_every_required_lib_has_a_driver` rather than being silently skipped.

Surfaces pinned, all against the REAL shipped hooks copied into a scratch
project — never against hook source text, which a comment could satisfy:

1. `post-file-edit.sh` — Edit/Write path: stderr line + the notice inside the
   hook's single `additionalContext` envelope, and still exit 0.
2. `post-bash-file-sync.sh` — Bash path: same two channels, still exit 0.
3. `session-start-retrieval-health.sh` — the SessionStart surface, so the
   condition is visible BEFORE the first edit of the session.
4. The `.ps1` flavour's copy of the set is the same set (skipped only where
   `pwsh` is absent, and never skipped under CI).

Plus the once-per-session rule (a hook that fires 200 times must not print 200
times), the per-lib WORDING (the notice body was hard-wired to one lib's role,
which would have been a false sentence for the other two), and the
counter-case: with every library present, none of this text appears anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402

HOOKS = REPO_ROOT / "templates" / "hooks"
LIB_SRC = HOOKS / "_lib"

#: The phrase every surface shares, whichever library is missing.
BROKEN = "broken install"


def _have_bash() -> bool:
    return shutil.which("bash") is not None


pytestmark = pytest.mark.skipif(not _have_bash(), reason="bash unavailable")


def _shell_table(function: str, *args: str) -> str:
    """Run one function of the SHIPPED table and return its stdout.

    Reading the set by EXECUTING its one home, rather than re-listing it here
    or grepping the source: a name in a comment cannot satisfy this, and a row
    added to that home reaches these tests with no edit.
    """
    bash = shutil.which("bash")
    assert bash is not None
    quoted = " ".join(f"'{a}'" for a in args)
    script = f'. "{LIB_SRC / "emit-context.sh"}"; {function} {quoted}'
    out = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


REQUIRED_LIBS = tuple(_shell_table("vco_required_hook_libs").split())
ROLES = {lib: _shell_table("vco_hook_lib_role", lib).strip() for lib in REQUIRED_LIBS}


def _edit_payload(project: Path) -> dict:
    node = project / "knowledge" / "node.md"
    node.write_text("# node\n", encoding="utf-8")
    return {
        "tool_name": "Write",
        "session_id": "sess-1",
        "agent_id": "",
        "agent_type": "",
        "tool_input": {"file_path": str(node)},
    }


def _bash_payload(project: Path) -> dict:
    (project / "knowledge" / "node.md").write_text("# node\n", encoding="utf-8")
    return {
        "tool_name": "Bash",
        "session_id": "sess-1",
        "tool_input": {"command": "cat > knowledge/node.md <<'EOF'\n# node\nEOF"},
    }


#: Which hook(s) each required library is on the critical path of, and the
#: payload that reaches it. `bash-write-targets` is the Bash hook's parser, so
#: the Edit hook cannot notice its absence — that asymmetry is the reason this
#: mapping is explicit rather than "every hook × every lib".
DRIVERS: dict[str, tuple[tuple[str, object], ...]] = {
    "route-touched-path": (
        ("post-file-edit.sh", _edit_payload),
        ("post-bash-file-sync.sh", _bash_payload),
    ),
    "bash-write-targets": (
        ("post-bash-file-sync.sh", _bash_payload),
    ),
    "code-extensions": (
        ("post-file-edit.sh", _edit_payload),
        ("post-bash-file-sync.sh", _bash_payload),
    ),
}

CASES = [
    pytest.param(lib, hook, builder, id=f"{lib}-{hook}")
    for lib, drivers in DRIVERS.items()
    for hook, builder in drivers
]


def test_every_required_lib_has_a_driver():
    """The anti-regression for MAJOR-2 itself.

    A fourth required lib added to `vco_required_hook_libs` must not be able
    to arrive here uncovered — which is exactly how the second and third ones
    stayed silent through a review that was about this class.
    """
    assert set(DRIVERS) == set(REQUIRED_LIBS), (
        "the shipped set and this file's drivers disagree: "
        f"shipped={sorted(REQUIRED_LIBS)} driven={sorted(DRIVERS)}"
    )
    assert len(REQUIRED_LIBS) >= 3


def _project(tmp_path: Path, *hook_names: str) -> Path:
    root = tmp_path / "proj"
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copytree(LIB_SRC, hooks / "_lib")
    for name in hook_names:
        shutil.copy(HOOKS / name, hooks / name)
    (root / "knowledge").mkdir()
    (root / "docs").mkdir()
    (root / ".claude" / "state").mkdir(parents=True)
    (root / ".claude" / "scripts").mkdir(parents=True)
    return root


def _break_lib(project: Path, lib: str) -> Path:
    """Rename one library — the exact shape a half-applied bundle leaves."""
    path = project / ".claude" / "hooks" / "_lib" / f"{lib}.sh"
    assert path.exists(), f"fixture did not stage {lib}.sh"
    moved = path.with_suffix(".sh.absent")
    path.rename(moved)
    return moved


def _env(project: Path) -> dict:
    """`child_env()`, not `os.environ.copy()` — v0.2.95 review MINOR-7.

    Two arms of this file failed under `env -u PYTHONPATH` before this change,
    and they failed for the WRONG reason: with no importable `vco_lib`,
    `post-bash-file-sync.sh`'s parser recovers no write targets and the hook
    exits at `[ -z "$PATHS" ]` — BEFORE the routing-lib check this file exists
    to pin. A test that passes only because the operator exported the right
    variable is not pinned, and its failure reads like a regression.
    `child_env` puts the checkout's import roots first, so the subject is the
    tree.

    (That early exit is correct layering, not a second defect: "no importable
    vco_lib" is a different broken-install condition, and the SessionStart
    ladder probe reports it as `KG write path: REFUSED`.)
    """
    env = child_env(
        CLAUDE_PROJECT_DIR=str(project),
        VCO_KG_SYNC_DEBOUNCE_SECONDS="0",
        VCT_INSTALL_ROOT=str(REPO_ROOT),
        PATH=os.path.dirname(sys.executable)
        + os.pathsep
        + os.environ.get("PATH", ""),
    )
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_VENV", None)
    return env


def _run(project: Path, hook: str, payload: dict) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(
        [bash, str(project / ".claude" / "hooks" / hook)],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=_env(project),
        timeout=90,
    )


def _envelope_text(stdout: str) -> str:
    """Concatenated `additionalContext` of every envelope on stdout."""
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctx = (doc.get("hookSpecificOutput") or {}).get("additionalContext")
        if ctx:
            out.append(ctx)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 1 + 2: the two write-side hooks, every required library
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("lib", "hook", "payload_builder"), CASES)
def test_a_renamed_lib_is_reported_on_both_channels(
    tmp_path: Path, lib: str, hook: str, payload_builder
) -> None:
    project = _project(tmp_path, hook)
    _break_lib(project, lib)

    result = _run(project, hook, payload_builder(project))

    assert result.returncode == 0, (
        f"{hook} must still exit 0 — a PostToolUse hook can never block.\n"
        f"stderr: {result.stderr}"
    )
    assert lib in result.stderr and BROKEN in result.stderr.lower(), (
        f"{hook} said nothing on stderr about the missing {lib}.\n"
        f"stderr: {result.stderr!r}"
    )
    envelope = _envelope_text(result.stdout)
    assert lib in envelope, (
        f"{hook} did not put the notice in an additionalContext envelope — "
        "plain PostToolUse stdout is discarded, so stderr alone never reaches "
        f"the model.\nstdout: {result.stdout!r}"
    )
    # The remediation must be a command the user can actually run.
    assert "install-bundle" in envelope and "--update" in envelope, envelope


@pytest.mark.parametrize(("lib", "hook", "payload_builder"), CASES)
def test_the_notice_names_what_that_particular_lib_does(
    tmp_path: Path, lib: str, hook: str, payload_builder
) -> None:
    """Per-lib WORDING, not one sentence reused for three files.

    The body was hard-wired to `route-touched-path`'s role ("every Edit, Write
    and CLI write is being parsed and then dropped"), which is a FALSE
    sentence for a missing `code-extensions` — the KG and docs legs are fine,
    the code-graph drain is the leg that stopped.
    """
    project = _project(tmp_path, hook)
    _break_lib(project, lib)

    result = _run(project, hook, payload_builder(project))

    assert ROLES[lib] in _envelope_text(result.stdout), (
        f"the notice for {lib} did not carry its own role sentence\n"
        f"want: {ROLES[lib]!r}\ngot: {result.stdout!r}"
    )


@pytest.mark.parametrize(("lib", "hook", "payload_builder"), CASES)
def test_the_notice_is_once_per_session(
    tmp_path: Path, lib: str, hook: str, payload_builder
) -> None:
    """A hook that fires 200 times a session must not print 200 times."""
    project = _project(tmp_path, hook)
    _break_lib(project, lib)

    first = _run(project, hook, payload_builder(project))
    second = _run(project, hook, payload_builder(project))

    assert lib in first.stderr, first.stderr
    assert lib not in second.stderr, (
        "the notice repeated within one session — the sentinel is not working"
    )
    assert lib not in _envelope_text(second.stdout)
    sentinels = list((project / ".claude" / "state").glob("route_lib_missing_*"))
    assert len(sentinels) == 1, sentinels
    # A DIFFERENT session must be told again.
    payload = payload_builder(project)
    payload["session_id"] = "sess-2"
    third = _run(project, hook, payload)
    assert lib in third.stderr, third.stderr


@pytest.mark.parametrize(
    ("hook", "payload_builder"),
    [
        ("post-file-edit.sh", _edit_payload),
        ("post-bash-file-sync.sh", _bash_payload),
    ],
)
def test_nothing_is_said_when_the_libs_are_present(
    tmp_path: Path, hook: str, payload_builder
) -> None:
    """Counter-case. Without it, a notice printed unconditionally would pass
    every assertion above."""
    project = _project(tmp_path, hook)
    result = _run(project, hook, payload_builder(project))
    assert result.returncode == 0, result.stderr
    assert BROKEN not in result.stderr.lower(), result.stderr
    assert BROKEN not in _envelope_text(result.stdout).lower()
    assert not list((project / ".claude" / "state").glob("route_lib_missing_*"))


# ---------------------------------------------------------------------------
# 3: the SessionStart surface — every required library, not just one
# ---------------------------------------------------------------------------

def _run_health(project: Path) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    assert bash is not None
    env = _env(project)
    # The retrieval probes talk to Weaviate; point them at a dead port so the
    # hook takes its "unknown" path quickly instead of hitting a live server.
    env["WEAVIATE_URL"] = "http://127.0.0.1:9"
    return subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "session-start-retrieval-health.sh")],
        input="{}",
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.mark.parametrize("lib", REQUIRED_LIBS)
def test_the_session_start_probe_names_every_missing_lib(
    tmp_path: Path, lib: str
) -> None:
    project = _project(tmp_path, "session-start-retrieval-health.sh")
    _break_lib(project, lib)

    result = _run_health(project)
    assert result.returncode == 0, result.stderr
    assert "KG write routing: BROKEN" in result.stdout, result.stdout
    assert lib in result.stdout, result.stdout
    assert ROLES[lib] in result.stdout, result.stdout
    assert "install-bundle" in result.stdout, result.stdout
    # The printed command must carry a real folder, not an empty one.
    assert f"--folder {project}" in result.stdout, result.stdout


def test_the_session_start_probe_is_quiet_when_the_libs_are_present(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "session-start-retrieval-health.sh")
    result = _run_health(project)
    assert result.returncode == 0, result.stderr
    assert "KG write routing: OK" in result.stdout, result.stdout
    for lib in REQUIRED_LIBS:
        assert f"_lib/{lib}.sh" in result.stdout, (
            f"the OK line does not account for {lib}", result.stdout,
        )
    assert "BROKEN" not in result.stdout, result.stdout


def test_the_probe_says_unknown_when_the_set_itself_is_missing(
    tmp_path: Path,
) -> None:
    """`emit-context.sh` holds the set. Without it the probe cannot know what
    to check — and must say so rather than print OK about a list it could not
    read (the failure mode this whole file exists for, one layer up)."""
    project = _project(tmp_path, "session-start-retrieval-health.sh")
    _break_lib(project, "emit-context")

    result = _run_health(project)
    assert result.returncode == 0, result.stderr
    assert "KG write routing: unknown" in result.stdout, result.stdout
    assert "emit-context.sh" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# 4: the two flavours declare the SAME set
# ---------------------------------------------------------------------------

def test_the_powershell_flavour_declares_the_same_set() -> None:
    """One set, two languages (C-tier mirror, locked here).

    Skipped where `pwsh` is absent — but REFUSED as a skip under CI, where the
    parity gate's whole value is that it ran.
    """
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        if os.environ.get("CI"):
            pytest.fail("pwsh missing under CI — this parity check must run")
        pytest.skip("pwsh unavailable")

    script = (
        f'. "{LIB_SRC / "emit-context.ps1"}"; '
        "Get-VcoRequiredHookLibs | ForEach-Object { Write-Output $_ }"
    )
    out = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert tuple(out.stdout.split()) == REQUIRED_LIBS, (
        "the .ps1 set drifted from the .sh set", out.stdout,
    )

    for lib in REQUIRED_LIBS:
        role_script = (
            f'. "{LIB_SRC / "emit-context.ps1"}"; Get-VcoHookLibRole "{lib}.ps1"'
        )
        role = subprocess.run(
            [pwsh, "-NoProfile", "-Command", role_script],
            capture_output=True, text=True, timeout=120,
        )
        assert role.returncode == 0, role.stderr
        # The two flavours' prose differs only where the .ps1 must avoid
        # non-ASCII (the em dash), so compare on that normalisation.
        assert role.stdout.strip().replace("--", "—") == ROLES[lib], (
            lib, role.stdout, ROLES[lib],
        )
