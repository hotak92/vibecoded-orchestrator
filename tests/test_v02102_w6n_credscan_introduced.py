# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-N — post-tool-security scans what the edit INTRODUCED.

Owner ruling 2026-10-12: ``post-tool-security.{sh,ps1}`` grepped the WHOLE
edited file on every Edit/Write, so the ``ghp_…`` test fixtures already in
``secrets.rs`` raised "Possible credential in secrets.rs: GitHub token" on
every unrelated edit. Now:

1. only the lines the call introduced are scanned (Edit ``new_string``,
   Write ``content``; Claude Code has no MultiEdit tool any more);
2. findings name the project-relative path, the line(s) and the shape label,
   never the value -- in the toast, the JSONL ledger and the model reminder;
3. a line carrying the explicit marker ``vco-secret-scan: test-placeholder``
   (one home: ``vco_lib.credential_shapes.TEST_PLACEHOLDER_MARKER``) is a
   declared placeholder; nothing is inferred from how a value looks;
4. a missing vocabulary still alerts loudly, and the ledger gets a row for
   every finding;
5. (W6-T, owner ruling 2026-10-12) every ``BEGIN ... PRIVATE KEY`` marker
   alerts whatever body follows it -- the v0.2.82 120-char body floor is gone
   ("secrets may not just be keys ... potentially shorter than 120 chars");
   only the explicit marker on the BEGIN line silences it.

Both siblings are run for real and must agree.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "templates" / "hooks"
SCRIPTS = REPO / "templates" / "scripts"

#: Synthetic: a classic-PAT prefix plus a single-character repeat. The tail
#: is what must never surface in any output.
TAIL = "Q" * 36
TOKEN = "ghp_" + TAIL
MARKER = "vco-secret-scan: test-placeholder"

_NOTIFY = """import sys
with open(sys.argv[0] + ".calls", "a", encoding="utf-8") as fh:
    fh.write(" | ".join(sys.argv[1:]) + "\\n")
"""

SIBLINGS = ["sh", "ps1"]


def _cmd(sibling: str, hooks_dir: Path = HOOKS) -> list:
    if sibling == "sh":
        return ["bash", str(hooks_dir / "post-tool-security.sh")]
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    return [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
            str(hooks_dir / "post-tool-security.ps1")]


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".claude" / "logs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    (proj / ".claude" / "scripts" / "notify.py").write_text(
        _NOTIFY, encoding="utf-8")
    (proj / "src").mkdir()
    return proj


class Run:
    def __init__(self, proc: subprocess.CompletedProcess, proj: Path):
        self.proc = proc
        ledger = proj / ".claude" / "logs" / "credential_alerts.jsonl"
        self.rows = [json.loads(ln) for ln in
                     (ledger.read_text(encoding="utf-8").splitlines()
                      if ledger.exists() else []) if ln.strip()]
        calls = proj / ".claude" / "scripts" / "notify.py.calls"
        self.toasts = calls.read_text(encoding="utf-8") if calls.exists() else ""
        self.everything = (proc.stdout + proc.stderr + self.toasts
                           + (ledger.read_text(encoding="utf-8")
                              if ledger.exists() else ""))

    @property
    def alerted(self) -> bool:
        return bool(self.rows)


def _run(sibling: str, proj: Path, tool: str, tool_input: dict,
         hooks_dir: Path = HOOKS) -> Run:
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCT_HOOK_LEAK_PROBE")}
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["STDERR_CAP_DISABLE"] = "1"
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool,
               "session_id": "sess-cs", "tool_input": tool_input}
    proc = subprocess.run(_cmd(sibling, hooks_dir), input=json.dumps(payload),
                          capture_output=True, text=True, env=env,
                          timeout=120, cwd=str(proj))
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return Run(proc, proj)


def _edit(proj: Path, before: str, old: str, new: str, **extra) -> dict:
    """Write the POST-edit file (PostToolUse sees the result) and return
    the Edit tool_input."""
    target = proj / "src" / "app.py"
    assert old in before
    target.write_text(before.replace(old, new, 1), encoding="utf-8")
    return {"file_path": str(target), "old_string": old, "new_string": new,
            **extra}


def _assert_never_the_value(run: Run) -> None:
    assert TAIL not in run.everything, "a credential value leaked into output"
    assert TOKEN not in run.everything


# ── 1. scope = what the edit introduced ──────────────────────────────────


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_unmarked_token_in_new_text_alerts_with_its_line(tmp_path, sibling):
    proj = _project(tmp_path)
    before = "import os\n\nVALUE = None\n"
    ti = _edit(proj, before, "VALUE = None",
               f'VALUE = None\nTOKEN = "{TOKEN}"')
    run = _run(sibling, proj, "Edit", ti)
    assert run.alerted, run.proc.stdout
    row = run.rows[-1]
    assert row["file"] == "src/app.py", row
    assert row["findings"] == [{"line": 4, "id": "github_token",
                                "label": "GitHub token"}], row
    assert "GitHub token" in row["patterns"]
    assert "src/app.py" in run.toasts and "line 4" in run.toasts, run.toasts
    assert "[Security alert]" in run.proc.stdout
    assert "src/app.py" in run.proc.stdout and "line 4" in run.proc.stdout
    _assert_never_the_value(run)


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_marked_placeholder_does_not_alert(tmp_path, sibling):
    proj = _project(tmp_path)
    before = "VALUE = None\n"
    ti = _edit(proj, before, "VALUE = None",
               f'VALUE = None\nTOKEN = "{TOKEN}"  # {MARKER}')
    run = _run(sibling, proj, "Edit", ti)
    assert not run.alerted, run.rows
    assert "[Security alert]" not in run.proc.stdout
    _assert_never_the_value(run)


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_marker_on_another_line_does_not_silence(tmp_path, sibling):
    """The marker covers its OWN line only."""
    proj = _project(tmp_path)
    before = "VALUE = None\n"
    ti = _edit(proj, before, "VALUE = None",
               f'# {MARKER}\nTOKEN = "{TOKEN}"')
    run = _run(sibling, proj, "Edit", ti)
    assert run.alerted and run.rows[-1]["findings"][0]["line"] == 2, run.rows


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_preexisting_token_untouched_by_unrelated_edit(tmp_path, sibling):
    """RED before the fix: the whole file was scanned on every edit."""
    proj = _project(tmp_path)
    before = f'TOKEN = "{TOKEN}"\n\ndef f():\n    return 1\n'
    ti = _edit(proj, before, "return 1", "return 2")
    run = _run(sibling, proj, "Edit", ti)
    assert not run.alerted, run.rows
    assert not run.toasts
    assert "[Security alert]" not in run.proc.stdout


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_preexisting_unmarked_token_on_an_edited_line_alerts(tmp_path,
                                                             sibling):
    proj = _project(tmp_path)
    before = f'X = 1\nTOKEN = "{TOKEN}"\n'
    ti = _edit(proj, before, f'"{TOKEN}"', f'"{TOKEN}"  # rotated')
    run = _run(sibling, proj, "Edit", ti)
    assert run.alerted
    assert run.rows[-1]["findings"][0]["line"] == 2, run.rows
    _assert_never_the_value(run)


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_write_scans_its_content(tmp_path, sibling):
    proj = _project(tmp_path)
    target = proj / "src" / "new.py"
    body = f'A = 1\nB = 2\nTOKEN = "{TOKEN}"\n'
    target.write_text(body, encoding="utf-8")
    run = _run(sibling, proj, "Write",
               {"file_path": str(target), "content": body})
    assert run.rows[-1]["findings"] == [
        {"line": 3, "id": "github_token", "label": "GitHub token"}]
    assert run.rows[-1]["file"] == "src/new.py"
    _assert_never_the_value(run)


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_deletion_introduces_nothing(tmp_path, sibling):
    proj = _project(tmp_path)
    before = f'TOKEN = "{TOKEN}"\nX = 1\n'
    ti = _edit(proj, before, "X = 1\n", "")
    run = _run(sibling, proj, "Edit", ti)
    assert not run.alerted, run.rows


# ── 4. safety properties kept ────────────────────────────────────────────


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_missing_vocabulary_is_a_loud_alert(tmp_path, sibling):
    proj = _project(tmp_path)
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    (hooks / "_lib" / "credshapes.sh").unlink()
    (hooks / "_lib" / "credshapes.ps1").unlink()
    shutil.copytree(SCRIPTS, tmp_path / "scripts")
    target = proj / "src" / "a.py"
    target.write_text("X = 1\n", encoding="utf-8")
    run = _run(sibling, proj, "Write",
               {"file_path": str(target), "content": "X = 1\n"}, hooks)
    assert run.alerted, (run.proc.stdout, run.proc.stderr)
    assert "UNAVAILABLE" in run.rows[-1]["patterns"], run.rows


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_missing_scan_helper_is_a_loud_alert(tmp_path, sibling):
    proj = _project(tmp_path)
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    target = proj / "src" / "a.py"
    target.write_text("X = 1\n", encoding="utf-8")
    run = _run(sibling, proj, "Write",
               {"file_path": str(target), "content": "X = 1\n"}, hooks)
    assert run.alerted and "UNAVAILABLE" in run.rows[-1]["patterns"], run.rows


def test_sh_no_python_is_a_loud_alert(tmp_path):
    """No interpreter = a broken install: the .sh must say the edit went
    unscanned (parity with the .ps1 'no Python interpreter' alert), never
    exit silently as if the edit were clean."""
    proj = _project(tmp_path)
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)
    # find-python.sh resolves nothing: $PY stays empty.
    (hooks / "_lib" / "find-python.sh").write_text("PY=\n", encoding="utf-8")
    target = proj / "src" / "a.py"
    target.write_text("X = 1\n", encoding="utf-8")
    run = _run("sh", proj, "Write",
               {"file_path": str(target), "content": "X = 1\n"}, hooks)
    assert "credential scanner UNAVAILABLE" in run.proc.stderr, run.proc.stderr
    envelope = json.loads(run.proc.stdout)
    assert "UNAVAILABLE" in envelope["hookSpecificOutput"]["additionalContext"]


_PEM_HEAD = "-----BEGIN EC " + "PRIVATE KEY-----"
_PEM_TAIL = "-----END EC " + "PRIVATE KEY-----"


@pytest.mark.parametrize("sibling", SIBLINGS)
@pytest.mark.parametrize("body, tail", [
    ("MHcCAQEE", True),                       # the secrets.rs-sized 13-char stub
    ("", True),                               # no body at all
    ("\n".join(["A" * 64] * 3), True),       # a full-size body
    ("short", False),                         # no END line either
], ids=["short-body", "empty-body", "full-body", "no-end"])
def test_every_pem_marker_alerts(tmp_path, sibling, body, tail):
    """No body-length floor: the BEGIN line alone is the finding."""
    proj = _project(tmp_path)
    end = f"\n{_PEM_TAIL}" if tail else ""
    ti = _edit(proj, "X = 1\n", "X = 1", f"X = 1\nK = '''{_PEM_HEAD}\n{body}{end}'''")
    run = _run(sibling, proj, "Edit", ti)
    assert run.alerted, run.rows
    assert run.rows[-1]["findings"] == [{"line": 2, "id": "pem_private_key", "label": "PEM private key"}]


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_pem_marker_silenced_only_by_the_marker_on_its_line(tmp_path, sibling):
    proj = _project(tmp_path)
    ti = _edit(proj, "X = 1\n", "X = 1",
               f"X = 1\nK = '''{_PEM_HEAD}  # {MARKER}\nMHcCAQEE\n{_PEM_TAIL}'''")
    assert not _run(sibling, proj, "Edit", ti).alerted

    proj2 = _project(tmp_path / "second")
    ti2 = _edit(proj2, "X = 1\n", "X = 1",
                f"X = 1\n# {MARKER}\nK = '''{_PEM_HEAD}\nMHcCAQEE  # {MARKER}\n{_PEM_TAIL}'''")
    run = _run(sibling, proj2, "Edit", ti2)
    assert run.alerted, "a marker on another line (before, or on the body) must not silence the BEGIN line"
    assert [f["line"] for f in run.rows[-1]["findings"]] == [3]


def test_no_body_floor_left_in_the_scan_helper():
    """The floor's constants and gate are gone from the one implementation."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("credscan_introduced_w6t", SCRIPTS / "credscan_introduced.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for gone in ("PEM_BODY_FLOOR", "PEM_WINDOW", "PEM_SHAPE_ID", "_pem_plausible"):
        assert not hasattr(mod, gone), gone
    import re
    shapes = [("pem_private_key", "PEM private key", re.compile("BEGIN [A-Z0-9 ]*PRIVATE KEY"))]
    text = f"{_PEM_HEAD}\n"
    assert mod.scan_lines(text, [1], shapes, MARKER) == [
        {"line": 1, "id": "pem_private_key", "label": "PEM private key"}]


@pytest.mark.parametrize("sibling", SIBLINGS)
def test_ledger_row_for_every_finding_event(tmp_path, sibling):
    """The toast is deduped; the ledger is not."""
    proj = _project(tmp_path)
    target = proj / "src" / "w.py"
    body = f'T = "{TOKEN}"\n'
    target.write_text(body, encoding="utf-8")
    ti = {"file_path": str(target), "content": body}
    _run(sibling, proj, "Write", ti)
    run = _run(sibling, proj, "Write", ti)
    assert len(run.rows) == 2, run.rows
    assert run.toasts.count("Security Alert") == 1, run.toasts


# ── sibling parity ───────────────────────────────────────────────────────


_PARITY = [
    ("edit_new", "X = 1\n", "X = 1", f'X = 1\nT = "{TOKEN}"'),
    ("edit_marked", "X = 1\n", "X = 1", f'X = 1\nT = "{TOKEN}"  # {MARKER}'),
    ("edit_unrelated", f'T = "{TOKEN}"\nY = 1\n', "Y = 1", "Y = 2"),
    ("edit_same_line", f'T = "{TOKEN}"\n', f'"{TOKEN}"', f'"{TOKEN}" # x'),
    ("edit_two_shapes", "X = 1\n", "X = 1",
     f'X = 1\nT = "{TOKEN}"\nAPI_KEY = "{"k" * 40}"'),
]


@pytest.mark.parametrize("name, before, old, new", _PARITY,
                         ids=[p[0] for p in _PARITY])
def test_sh_and_ps1_give_the_same_verdict(tmp_path, name, before, old, new):
    if not shutil.which("pwsh"):
        pytest.skip("pwsh not installed")
    verdicts = []
    for sibling in SIBLINGS:
        proj = _project(tmp_path / sibling)
        run = _run(sibling, proj, "Edit", _edit(proj, before, old, new))
        row = run.rows[-1] if run.rows else {}
        verdicts.append((row.get("file"), row.get("patterns"),
                         row.get("findings"), run.toasts.strip()))
        _assert_never_the_value(run)
    assert verdicts[0] == verdicts[1], verdicts


# ── the marker's one home ────────────────────────────────────────────────


def test_marker_mirrors_match_the_ssot():
    sys.path.insert(0, str(REPO))
    from vco_lib.credential_shapes import TEST_PLACEHOLDER_MARKER
    assert TEST_PLACEHOLDER_MARKER == MARKER
    sh = subprocess.run(
        ["bash", "-c", '. "$1"; printf %s "$CREDSHAPES_TEST_PLACEHOLDER_MARKER"',
         "_", str(HOOKS / "_lib" / "credshapes.sh")],
        capture_output=True, text=True, check=True)
    assert sh.stdout == TEST_PLACEHOLDER_MARKER
    pwsh = shutil.which("pwsh")
    if pwsh:
        ps = subprocess.run(
            [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command",
             f". '{HOOKS / '_lib' / 'credshapes.ps1'}'; "
             "[Console]::Out.Write((Get-CredShapesTestPlaceholderMarker))"],
            capture_output=True, text=True, check=True)
        assert ps.stdout == TEST_PLACEHOLDER_MARKER
