# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""R42 sweep (v0.2.92): every PRINTED REMEDY must be pasteable on the machine
it is printed on.

R42 is binding — *Windows parity is achieved by WRITING the ``.ps1``, never by
narrowing the feature* — and a remedy a Windows user cannot paste narrows the
feature just as effectively. The condition is detected on their machine, the
entry is written on their machine, and the one command that would resolve it
is in a shell they do not have.

What this file does NOT do: scan source for forbidden substrings. That form
is satisfied by a comment mentioning ``&&`` and blind to a remedy assembled
from f-strings. Every test here RENDERS a real emitter — with ``os.name``
faked to ``"nt"`` — and asserts on the produced text, so a future emitter that
reintroduces the shape fails here whatever it looks like in source.

The three shapes, and why each is a defect and not a style nit:

* ``a && b`` — Windows PowerShell 5.1 (the ``powershell.exe`` on every
  Windows 10/11 box, and what an "elevated terminal" usually opens) rejects
  ``&&`` as a syntax error. Not a warning; the line does not run.
* ``'single quotes'`` — cmd.exe does not strip them, so the quotes reach the
  program as part of the argument: a quoted path becomes a path nobody has.
* POSIX-only tools/paths — ``cp``/``mkdir -p``/``chmod``/``ln -s``/``/tmp``/
  ``~/.local/bin`` and bash script wrappers whose Windows sibling is ``.ps1``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib import remedy_shell  # noqa: E402


@pytest.fixture
def on_windows(monkeypatch):
    """Render as if the user is sitting at a Windows box.

    ``sys.platform`` is the seam every OS branch in this repo's remediation
    code reads (``vco_lib.doctor``'s inline branches and
    ``remedy_shell.is_windows`` alike), so ONE patch flips them together.
    Deliberately NOT ``os.name``: pathlib picks ``WindowsPath`` off that, and
    ``WindowsPath`` refuses to instantiate on a POSIX host — the test would
    fail for a reason that has nothing to do with the remedy.
    """
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


def test_steps_never_emits_the_and_operator(on_windows):
    rendered = remedy_shell.steps("git fetch x", "git reset --hard y")
    assert "&&" not in rendered
    assert rendered.splitlines() == ["git fetch x", "git reset --hard y"]


def test_quote_uses_double_quotes_on_windows(on_windows):
    assert remedy_shell.quote(r"C:\Program Files\vco") == r'"C:\Program Files\vco"'
    # Nothing to escape → left bare, so the common case stays readable.
    assert remedy_shell.quote(r"C:\vco") == r"C:\vco"
    assert "'" not in remedy_shell.quote("refs/*:refs/heads/x/*")


def test_quote_uses_posix_quoting_off_windows(on_posix):
    assert remedy_shell.quote("/opt/my vco") == "'/opt/my vco'"


def test_script_invocation_targets_the_ps1_sibling_on_windows(on_windows):
    line = remedy_shell.script_invocation(
        r"C:\proj", ".claude/scripts/kg-sync", "--all",
    )
    assert "kg-sync.ps1" in line
    assert line.startswith("powershell.exe -NoProfile -ExecutionPolicy Bypass -File")
    assert line.endswith("--all")


def test_script_invocation_is_the_bare_wrapper_off_windows(on_posix):
    line = remedy_shell.script_invocation(
        "/proj", ".claude/scripts/kg-sync", "--all",
    )
    assert line == "/proj/.claude/scripts/kg-sync --all"


def test_copy_tree_command_never_overwrites_on_either_platform(on_windows):
    win = remedy_shell.copy_tree_command(r"C:\old\memory", r"C:\new\memory")
    # /XN /XO /XC = skip newer, older and changed — i.e. never clobber.
    assert win.startswith("robocopy ")
    for flag in ("/E", "/XC", "/XN", "/XO"):
        assert flag in win
    assert "&&" not in win


# ---------------------------------------------------------------------------
# The emitters, rendered
# ---------------------------------------------------------------------------

#: Tools with no Windows equivalent that must not appear in a remedy rendered
#: for a Windows user. (``cd`` and ``git`` are fine — both exist there.)
#
#: ``/tmp`` is NOT in this list: a caller may legitimately pass a path under
#: it (a test fixture does), and the shape that matters is an INVENTED
#: ``/tmp`` the emitter wrote itself — pinned directly in
#: ``test_shadow_remediation_never_cds_into_tmp`` below, which is the instance
#: this sweep started from.
_POSIX_ONLY = (
    "mkdir -p", "cp -r", "cp -a", "ln -s", "chmod ", "sudo ", "df -h",
    "du -s", "~/.local/bin", "readlink -f",
)


def _command_lines(block: str) -> list[str]:
    """The lines a user actually PASTES, separated from the prose.

    Every remediation block in this repo follows one idiom: prose is ``# ``
    plus one space, a command is indented under it (``#   git ...``) or
    stands alone with no ``#`` at all. Checking prose would be wrong in both
    directions — it would flag ``rejects `cd X && Y``` (a sentence warning
    against the very shape) and it would say nothing about the command below
    it.
    """
    out = []
    for line in block.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            body = line.lstrip()[1:]
            if body.startswith("   "):          # `#   <command>`
                out.append(body.strip())
            continue
        out.append(line.strip())                # bare command line
    return out


def _assert_pasteable_on_windows(block: str, label: str) -> None:
    for line in _command_lines(block):
        assert "&&" not in line, (
            f"{label}: `&&` is a syntax error in PowerShell 5.1:\n{line}")
        for tool in _POSIX_ONLY:
            assert tool not in line, (
                f"{label}: POSIX-only `{tool}` in:\n{line}")
        # Single-quoted ARGUMENTS: cmd.exe hands the quotes to the program.
        assert not re.search(r"(^|\s)'[^']*'(\s|$)", line), (
            f"{label}: cmd.exe does not strip single quotes:\n{line}"
        )


def test_doctor_reattach_remediation_is_pasteable_on_windows(on_windows):
    """The detached-HEAD advice. Its ancestry check used to end in
    ``&& echo 'upstream contains this commit'`` — the one line telling a user
    whether reattaching is SAFE, un-runnable on the OS whose launcher offers
    the one-click version of the same operation."""
    from vco_lib import doctor

    facts = doctor.SourceFacts(
        is_git_toplevel=True, detached=True, branch="main",
        head_sha="deadbeefcafe0000", local_branch_exists=True,
    )
    block = doctor._reattach_remediation(Path("/repo"), facts)
    _assert_pasteable_on_windows(block, "_reattach_remediation")
    # The ancestry question is still ASKED — the fix must not have dropped it.
    assert "merge-base --is-ancestor" in block
    assert "EXIT" in block or "exit code" in block.lower()


def test_doctor_stale_vct_remediation_is_pasteable_on_windows(on_windows):
    """Reachable on Windows: `vct` is extensionless, and `shutil.which` on
    Windows checks the bare name in addition to the PATHEXT variants, so a
    user who followed the old copy-onto-PATH docs and runs it under Git Bash
    lands here."""
    from vco_lib import doctor

    # POSIX-shaped inputs even for the Windows rendering: `display_path`
    # absolutises, and a `C:\...` literal on a POSIX test host absolutises
    # against the CWD — which would let this test pass while asserting on
    # mangled paths.
    checkout = Path("/vco/tools/vct-secrets/vct")
    block = doctor._stale_vct_remediation(checkout, "/opt/bin/vct")
    _assert_pasteable_on_windows(block, "_stale_vct_remediation")
    assert "Copy-Item -Recurse -Force" in block
    # The copy must name the checkout TREE as source (lib/ rides along) and
    # the DEPLOYED file's directory as destination — a remedy that copied the
    # single file would leave the stale lib/ behind and fix nothing.
    copy_line = next(
        line for line in _command_lines(block) if line.startswith("Copy-Item")
    )
    assert str(checkout.parent) in copy_line
    assert "/opt/bin" in copy_line
    assert "/opt/bin/vct" not in copy_line, (
        "the destination must be the DIRECTORY, not the stale file itself")


def test_shadow_remediation_never_cds_into_tmp():
    """The named instance this sweep started from: the shadowed-``vco_lib``
    verify line used to be prefixed with ``cd /tmp &&``. cmd.exe has no
    ``/tmp`` and PowerShell maps it to a PSDrive that may not exist, so the
    command that PROVES the repair worked was un-runnable exactly where the
    class is reported. Asserted on BOTH renderings — the fix was to delete
    the ``cd`` (``python -I`` already drops cwd and PYTHONPATH from
    ``sys.path``), so neither platform should reintroduce one."""
    from vco_lib import doctor

    # A root OUTSIDE /tmp, so any `/tmp` in the output was invented here.
    root = Path("/opt/vco-checkout")
    for platform in ("win32", "linux"):
        with mock.patch("vco_lib.doctor.sys.platform", platform):
            block = doctor._vco_lib_shadow_remediation(root, "/venv/lib/vco_lib")
        for line in _command_lines(block):
            assert "/tmp" not in line, f"invented /tmp on {platform}:\n{line}"
        assert "-I " in block, (
            "the isolation flag is what made the `cd` unnecessary; losing it "
            "would bring the `cd` back")


def test_doctor_currency_and_shadow_and_disk_stay_pasteable(on_windows):
    """The three blocks a previous round already OS-branched — pinned here so
    the branch cannot be lost in a later edit."""
    from vco_lib import doctor

    facts = doctor.SourceFacts(is_git_toplevel=True, branch="main")
    _assert_pasteable_on_windows(
        doctor._currency_remediation(Path("/repo"), facts), "_currency")
    _assert_pasteable_on_windows(
        doctor._npx_remediation(True), "_npx (npm present)")
    _assert_pasteable_on_windows(
        doctor._npx_remediation(False), "_npx (no npm)")


def test_hard_cut_restore_command_is_pasteable_on_windows(on_windows, tmp_path):
    """The restore path out of a hard cut. A hard cut is version-gated, not
    OS-gated, so half its population reads this on Windows — and this is the
    command that undoes it."""
    import subprocess

    from vco_lib import hard_cut

    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "install.py").write_text("# fake\n")
    vct = tmp_path / ".vct"
    vct.mkdir()

    def _runner(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "", "")

    captured = {}

    def _writer(*, clone_root, bundle_path, from_version, to_version, restore_cmd):
        captured["cmd"] = restore_cmd
        return True

    hard_cut.hard_cut(
        "0.2.0", "0.3.0",
        clone_root=clone, vct_root=vct, project_id=None,
        stamp="20260905T000000Z", runner=_runner, deferral_writer=_writer,
        migration_runner=lambda **kw: None, now_ms=1,
    )
    assert "cmd" in captured, "the hard cut never reached its deferral writer"
    _assert_pasteable_on_windows(captured["cmd"], "hard_cut restore_cmd")
    assert "restored-pre-hardcut" in captured["cmd"]
    # Two lines, and the refspec is not single-quoted.
    assert len(captured["cmd"].splitlines()) == 2


def test_project_move_memory_copy_is_pasteable_on_windows(on_windows):
    """Carrying auto-memory across a move. Moving a project folder is if
    anything MORE common on Windows."""
    from vco_lib import project_move

    report = project_move.harness_state_report(
        Path("/old/proj"), Path("/new/proj"), home=Path("/home/u"),
    )
    _assert_pasteable_on_windows(
        report["memory_copy_command"], "memory_copy_command")


def test_schema_regenerate_reingest_command_is_pasteable_on_windows(on_windows):
    """A dropped-but-not-re-ingested collection is EMPTY until this runs, so
    on Windows the data stayed missing behind an un-pasteable fix."""
    from vco_lib import schema_regenerate

    kg = schema_regenerate._reingest_remediation_command(
        "kg_collection", Path(r"C:\proj"), "Acme_KnowledgeGraph")
    cg = schema_regenerate._reingest_remediation_command(
        "codegraph_collection", Path(r"C:\proj"), "Acme_CodeFunction")
    _assert_pasteable_on_windows(kg, "reingest kg")
    _assert_pasteable_on_windows(cg, "reingest codegraph")
    assert "kg-sync.ps1" in kg and "--all" in kg
    assert "code-graph-analyze.ps1" in cg and "--force-recreate" in cg


def test_chunker_resync_remedy_is_pasteable_on_windows(on_windows):
    """The remedy v0.2.92 writes into EVERY pre-existing project's ledger.

    It used to be ``cd <folder>`` + two relative ``.claude/scripts/`` names —
    POSIX in three ways at once on the one remediation with the widest reach
    of the release. It must name the ``.ps1`` wrappers absolutely.
    """
    from vco_lib import chunker_revision

    block = chunker_revision.resync_commands(Path(r"C:\proj"), "# tail")
    _assert_pasteable_on_windows(block, "chunker resync")
    assert "kg-sync.ps1" in block and "--all" in block
    assert "code-graph-analyze.ps1" in block
    assert "--from-resolver --force-recreate" in block
    # No `cd` precondition survives: the wrapper is absolute and the analyzer
    # takes the folder as its positional repo_path.
    assert not any(line.startswith("cd ") for line in _command_lines(block))


def test_chunker_resync_remedy_shape_on_posix(on_posix):
    """POSIX keeps naming the bash wrappers, and the identity flag is not a
    Windows-only nicety — ``--force-recreate`` DROPS five classes on both."""
    from vco_lib import chunker_revision

    block = chunker_revision.resync_commands(Path("/proj"), "# tail")
    lines = _command_lines(block)
    assert "/proj/.claude/scripts/kg-sync --all" in lines
    assert (
        "/proj/.claude/scripts/code-graph-analyze /proj "
        "--from-resolver --force-recreate" in lines
    )


def test_schema_regenerate_reingest_command_unchanged_shape_on_posix(on_posix):
    """The POSIX rendering keeps naming the bash wrappers and their flags —
    the fix must not have traded one platform's breakage for the other's."""
    from vco_lib import schema_regenerate

    kg = schema_regenerate._reingest_remediation_command(
        "kg_collection", Path("/proj"), "Acme_KnowledgeGraph")
    cg = schema_regenerate._reingest_remediation_command(
        "codegraph_collection", Path("/proj"), "Acme_CodeFunction")
    assert kg == "/proj/.claude/scripts/kg-sync --all"
    assert cg.startswith("/proj/.claude/scripts/code-graph-analyze ")
    assert "--force-recreate" in cg


def test_windows_reserved_port_advice_avoids_the_and_operator(monkeypatch):
    """Emitted for Windows users BY DEFINITION, and introduced as "run this in
    an ELEVATED terminal" — which on Windows 10/11 opens PowerShell 5.1, the
    shell that rejects `&&`.

    Rendered, not grepped: the OS gate, the netsh probe and the elevation
    check are the three facts this emitter reads from the machine, so they are
    injected and the real emit path runs.
    """
    from vco_lib import windows_reserved_ports as wrp

    monkeypatch.setattr(wrp, "is_windows", lambda: True)
    monkeypatch.setattr(wrp, "query_excluded_ranges", lambda: [(49000, 51000)])
    monkeypatch.setattr(wrp, "is_elevated", lambda: False)

    emitted: list[str] = []
    unresolved = wrp.check_ports(
        [("weaviate", 50000)], auto_reserve=False, log=emitted.append,
    )
    assert unresolved, "the conflict must be reported to the caller"
    joined = "\n".join(emitted)
    assert "net stop winnat" in joined and "net start winnat" in joined
    for line in emitted:
        assert "&&" not in line, f"`&&` in Windows advice:\n{line}"

    # The SAME advice is printed by the ensure-containers PowerShell hook,
    # which cannot be executed on the Linux CI host — so its copy is pinned
    # textually. (Two homes for one message is pre-existing: the hook must
    # warn before any Python runs.)
    hook = (REPO_ROOT / "templates" / "hooks" / "ensure-containers.ps1").read_text(
        encoding="utf-8")
    assert "net stop winnat && net start winnat" not in hook
    assert '"    net stop winnat"' in hook and '"    net start winnat"' in hook
