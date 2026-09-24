# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""A CLI write must sync exactly like an Edit/Write (v0.2.95, lane F10).

Two halves:

* ``vco_lib.bash_write_targets`` — the parse. Unit-tested per shape, with
  the DECISION on both sides of every branch: a heredoc into ``knowledge/``
  is a target, the same heredoc into ``/tmp`` is not; ``sed -i`` on a docs
  page is a target, ``sed -n`` on the same page is not.
* ``templates/hooks/post-bash-file-sync.sh`` — the wiring. Driven END TO
  END against a scratch project with a stubbed ``kg-sync``: the assertions
  are on what the hook actually spawned, never on the hook's source text (a
  name in a comment satisfies a grep; a stub recording its argv does not).

The routing itself is NOT re-tested here — it is the same
``_lib/route-touched-path.sh`` post-file-edit.sh calls, whose behaviour is
pinned by tests/test_v0292_kg_sync_gate_selfcontained.py and
tests/test_post_file_edit_hk1_v0273.py. What IS tested here is that a Bash
command reaches it.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib.bash_write_targets import (  # noqa: E402
    collect_candidates,
    command_has_write_shape,
    extract_write_targets,
    prebash_query_parts,
    read_scan_watermark,
    scan_recent_writes,
    should_fallback_scan,
    strip_heredocs,
)

HOOKS = REPO_ROOT / "templates" / "hooks"
HOOK_SH = HOOKS / "post-bash-file-sync.sh"
LIB_SRC = HOOKS / "_lib"


# ===========================================================================
# The parse — every branch gets BOTH cases
# ===========================================================================

@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Redirections.
        ("cat > knowledge/foo.md <<'EOF'\n# node\nEOF", ["knowledge/foo.md"]),
        ("echo hi >> docs/notes.md", ["docs/notes.md"]),
        ("echo hi >docs/notes.md", ["docs/notes.md"]),
        # …and the redirect shapes that are NOT file writes.
        ("pytest tests/ -q 2>&1 | tail -20", []),
        ("ls -la > /dev/null", []),
        ("make 2>/dev/null", []),
        # In-place editors.
        ("sed -i 's/a/b/' vco_lib/foo.py", ["vco_lib/foo.py"]),
        ("sed --in-place 's/a/b/' docs/y.md", ["docs/y.md"]),
        ("perl -i -pe 's/a/b/' f.md", ["f.md"]),
        # …and the same tools NOT writing.
        ("sed -n '1,5p' docs/x.md", []),
        ("perl -ne 'print' docs/x.md", []),
        # Copy-shaped verbs.
        ("cp a.md docs/b.md", ["docs/b.md"]),
        ("cp a.md docs/", ["docs/a.md"]),
        ("cp a.md b.md docs/", ["docs/a.md", "docs/b.md"]),
        ("install -m 644 src.md docs/dst.md", ["docs/dst.md"]),
        ("mv old.md knowledge/new.md", ["knowledge/new.md"]),
        # …and the ones that create no file.
        ("install -d docs/sub", []),
        ("cp a.md", []),
        # tee / touch / dd.
        ("tee knowledge/a.md knowledge/b.md", ["knowledge/a.md", "knowledge/b.md"]),
        ("touch knowledge/new.md", ["knowledge/new.md"]),
        ("dd if=/dev/zero of=disk.img bs=1M", ["disk.img"]),
        # Long --output is unambiguous; short -o is not and is NOT read.
        ("python -m vco_lib.foo --output build/out.json", ["build/out.json"]),
        ("curl -o file.txt https://example.test/x", []),
        ("ssh -o StrictHostKeyChecking=no host", []),
        # Chains, wrappers, env prefixes, bash -c — via the shared walker.
        ('bash -c "echo hi > knowledge/inner.md"', ["knowledge/inner.md"]),
        ("KEY=val nice -n 10 sed -i 's/x/y/' docs/z.md", ["docs/z.md"]),
        ("sudo tee -a /etc/hosts", ["/etc/hosts"]),
        # Expansion we cannot resolve is dropped, not guessed.
        ("cat > $TARGET", []),
        ("cp a.md $DEST/", []),
        ("cp knowledge/*.md /tmp/", []),
        # Commands that write nothing at all.
        ("git status --short", []),
        ("grep -rn 'migrate_collections' vco_lib/", []),
    ],
)
def test_collect_candidates(command: str, expected: list[str]) -> None:
    assert collect_candidates(command) == expected


def test_heredoc_body_cannot_forge_a_redirect_target() -> None:
    """A `>` INSIDE a heredoc body is prose, not a redirection.

    Without body-stripping, `see a > b` in the body of a node would route a
    file named `b` into the KG.
    """
    command = "cat > knowledge/foo.md <<'EOF'\n# node\nsee a > b\nEOF"
    assert collect_candidates(command) == ["knowledge/foo.md"]
    stripped, bodies = strip_heredocs(command)
    assert bodies == ["# node\nsee a > b"]
    assert ">" in stripped  # the real redirection survives


def test_unterminated_heredoc_marker_does_not_swallow_the_command() -> None:
    """`echo "a << b"` must not eat every following line.

    An opener whose terminator never appears is treated as literal text.
    """
    command = 'echo "a << b" && echo z > docs/d.md'
    assert collect_candidates(command) == ["docs/d.md"]


def test_cd_retargets_a_relative_write() -> None:
    """`cd /tmp && cat > knowledge/x.md` writes /tmp/knowledge/x.md."""
    assert collect_candidates("cd /tmp && cat > knowledge/x.md") == [
        "/tmp/knowledge/x.md"
    ]
    assert collect_candidates("cd sub && echo x > y.md") == ["sub/y.md"]


def test_an_unresolvable_cd_drops_relative_targets() -> None:
    """Doing nothing beats guessing: after `cd "$D"` the shell's directory
    is unknown, so a relative target could be any file on the machine."""
    assert collect_candidates('cd "$D" && echo x > knowledge/y.md') == []
    # An ABSOLUTE target is still knowable and is kept.
    assert collect_candidates('cd "$D" && echo x > /srv/k/y.md') == ["/srv/k/y.md"]


def test_containment_and_existence_are_the_security_boundary(tmp_path: Path) -> None:
    """`sed -i /etc/passwd` collects a candidate and is then DROPPED."""
    (tmp_path / "knowledge").mkdir()
    node = tmp_path / "knowledge" / "n.md"
    node.write_text("x", encoding="utf-8")

    out = extract_write_targets(
        "sed -i 's/a/b/' /etc/passwd knowledge/n.md",
        project_root=str(tmp_path),
        require_exists=True,
    )
    assert out == [str(node)]


def test_require_exists_separates_the_two_surfaces(tmp_path: Path) -> None:
    """PostToolUse demands the file is on disk; PreToolUse must not."""
    (tmp_path / "knowledge").mkdir()
    cmd = "cat > knowledge/not-yet.md <<EOF\nx\nEOF"
    assert extract_write_targets(cmd, str(tmp_path), require_exists=True) == []
    assert extract_write_targets(cmd, str(tmp_path), require_exists=False) == [
        str(tmp_path / "knowledge" / "not-yet.md")
    ]


def test_targets_are_capped(tmp_path: Path) -> None:
    """A pathological command must not fan out into hundreds of syncs."""
    (tmp_path / "knowledge").mkdir()
    names = []
    for i in range(50):
        p = tmp_path / "knowledge" / f"n{i}.md"
        p.write_text("x", encoding="utf-8")
        names.append(f"knowledge/n{i}.md")
    cmd = "touch " + " ".join(names)
    out = extract_write_targets(cmd, str(tmp_path), require_exists=True, limit=32)
    assert len(out) == 32


# ===========================================================================
# The unparseable-write fallback
# ===========================================================================

#: Read-only commands that NAME a scanned directory. Each one used to pass
#: the prefilter, start Python, walk knowledge/ + docs/ and re-route every
#: file touched in the previous 300 s — the v0.2.95 review's MAJOR-3.
READ_ONLY_MENTIONS = [
    "cat docs/x.md",
    "less knowledge/a.md",
    "grep -rn foo knowledge/",
    "head -20 docs/README.md",
    "wc -l knowledge/notes.md",
    "ls docs/",
    "ls -la knowledge/concepts/",
    "g" "it diff docs/x.md",
    "g" "it log --oneline knowledge/",
]

#: Opaque writers: the command wrote something the TEXT cannot express, and
#: it could have landed in a scanned directory. These must still scan.
OPAQUE_WRITES = [
    "python -c \"open('knowledge/x.md','w')\"",
    "python3 -c \"open('docs/x.md','w')\"",
    "perl -e 'open F, \">docs/x.md\"'",
    "patch -p1 < docs.diff && echo done",
    "g" "it checkout -- docs/x.md",
    "rsync -a backup/ knowledge/",
]


@pytest.mark.parametrize("command", READ_ONLY_MENTIONS)
def test_a_read_only_mention_of_docs_neither_spawns_nor_scans(command: str) -> None:
    """Naming a directory is not writing to it.

    Both halves are asserted: the shape predicate says "no write", and the
    scan gate therefore says "no scan". The hook-level consequence (no
    interpreter at all) is pinned end to end further down.
    """
    assert not command_has_write_shape(command), command
    assert not should_fallback_scan(command, []), command


@pytest.mark.parametrize("command", OPAQUE_WRITES)
def test_an_opaque_write_still_scans(command: str) -> None:
    """The counter-case: narrowing the trigger must not close the hole the
    fallback exists for."""
    assert command_has_write_shape(command), command
    assert should_fallback_scan(command, []), command


def test_fallback_scan_triggers_only_on_an_opaque_shape() -> None:
    assert should_fallback_scan("python - <<'EOF'\nopen('x','w')\nEOF", [])
    assert should_fallback_scan("python -c \"open('a','w')\"", [])
    assert should_fallback_scan("sed -i 's/a/b/' knowledge/x.md", [])
    # NOT triggered: nothing opaque, or the parser already found the target.
    assert not should_fallback_scan("ls -la", [])
    assert not should_fallback_scan("echo hi > /tmp/x.txt", [])
    assert not should_fallback_scan("echo hi > docs/a.md", ["/p/docs/a.md"])


def test_scan_is_bounded_to_knowledge_and_docs(tmp_path: Path) -> None:
    for sub in ("knowledge", "docs", "vco_lib"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "f.md").write_text("x", encoding="utf-8")
    (tmp_path / "vco_lib" / "m.py").write_text("x", encoding="utf-8")

    found = scan_recent_writes(str(tmp_path), since_ts=time.time() - 60)
    rel = sorted(os.path.relpath(f, tmp_path) for f in found)
    assert rel == [os.path.join("docs", "f.md"), os.path.join("knowledge", "f.md")]


def test_first_ever_scan_does_not_sweep_the_whole_tree(tmp_path: Path) -> None:
    """No watermark → a 60 s lookback, not "everything ever written".

    The failure this prevents is a first triggered scan re-embedding a
    1 000-node knowledge/ in one burst.
    """
    state = tmp_path / "state" / "bash_write_scan.ts"
    now = 1_000_000.0
    assert read_scan_watermark(str(state), now=now) == now - 60
    state.parent.mkdir(parents=True)
    state.write_text("1", encoding="utf-8")  # ancient
    # …and an ancient watermark is floored at a 300 s lookback.
    assert read_scan_watermark(str(state), now=now) == now - 300


# ===========================================================================
# The pre-bash query shape (G2)
# ===========================================================================

def test_prebash_query_parts_uses_the_target_and_its_heredoc_body() -> None:
    target, snippet = prebash_query_parts(
        "cat > knowledge/retrieval-tiers.md <<'EOF'\nScore driven tiers\nEOF",
        project_root="/proj",
    )
    assert target == "/proj/knowledge/retrieval-tiers.md"
    assert snippet == "Score driven tiers"


def test_prebash_withholds_a_secret_shaped_body() -> None:
    """The snippet reaches the retrieval subprocess's argv, where `ps` can
    read it. A body carrying a credential shape is withheld."""
    target, snippet = prebash_query_parts(
        "cat > knowledge/x.md <<'EOF'\nAPI_KEY=sk-not-a-real-value\nEOF",
        project_root="/proj",
    )
    assert target == "/proj/knowledge/x.md"
    assert snippet == ""


def test_prebash_withholds_a_body_outside_knowledge_and_docs() -> None:
    target, snippet = prebash_query_parts(
        "cat > .env <<'EOF'\nSOMEVALUE=1\nEOF", project_root="/proj"
    )
    assert target == "/proj/.env"
    assert snippet == ""


def test_prebash_is_empty_when_no_target_is_recoverable() -> None:
    assert prebash_query_parts("git status --short", "/proj") == ("", "")


# ===========================================================================
# The wiring — driven end to end
# ===========================================================================

def _have_bash() -> bool:
    return shutil.which("bash") is not None


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copytree(LIB_SRC, hooks / "_lib")
    shutil.copy(HOOK_SH, hooks / "post-bash-file-sync.sh")
    (root / "knowledge").mkdir()
    (root / "docs").mkdir()
    (root / "vco_lib").mkdir()
    (root / ".claude" / "state").mkdir(parents=True)
    (root / ".claude" / "scripts").mkdir(parents=True)
    return root


def _stub_kg_sync(project: Path, marker: Path) -> None:
    kg_sync = project / ".claude" / "scripts" / "kg-sync"
    kg_sync.write_text(
        "#!/usr/bin/env bash\n" f'echo "KG-SYNC-RAN: $*" >> {marker}\n',
        encoding="utf-8",
    )
    kg_sync.chmod(0o755)


def _run_hook(project: Path, command: str, session: str = "s-1",
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    assert bash is not None
    # `child_env()` — the ONE home for a child process's environment — and NOT
    # `os.environ.copy()` (v0.2.95 review MINOR-7). The hook spawns
    # `python -m vco_lib.bash_write_targets` with `PYTHONPATH="$root:$PYTHONPATH"`,
    # so with an unset/foreign PYTHONPATH the parser cannot import `vco_lib`,
    # returns nothing, and this file's write-path tests fail — for a reason that
    # reads exactly like a regression in the hook. Measured on this tree with
    # `env -u PYTHONPATH`: 7 failed. `child_env` puts the checkout's two import
    # roots FIRST, so the test pins the tree instead of the operator's shell.
    env = child_env(
        CLAUDE_PROJECT_DIR=str(project),
        # Window 0 = run the sync immediately, so the assertion does not race a
        # 5 s debounce window.
        VCO_KG_SYNC_DEBOUNCE_SECONDS="0",
        KG_COLLECTION="Foo_KnowledgeGraph",
        DEVELOPMENT_COLLECTION="Foo_Development",
        VCT_INSTALL_ROOT=str(REPO_ROOT),
        PATH=os.path.dirname(sys.executable)
        + os.pathsep
        + os.environ.get("PATH", ""),
    )
    env.pop("VCT_DISABLE_HOOKS", None)
    env.pop("VCT_VENV", None)
    if extra_env:
        env.update(extra_env)
    payload = {
        "tool_name": "Bash",
        "session_id": session,
        "tool_input": {"command": command},
    }
    return subprocess.run(
        [bash, str(project / ".claude" / "hooks" / "post-bash-file-sync.sh")],
        input=json.dumps(payload),
        cwd=str(project),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _await(path: Path, timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8")
        time.sleep(0.1)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_the_hook_is_registered_on_both_operating_systems() -> None:
    """A hook nobody registers is a file, not a feature.

    Both siblings must also exist — a .sh-only landing leaves native-Windows
    users (no WSL) with the gap this lane closed still open.
    """
    assert HOOK_SH.exists()
    assert (HOOKS / "post-bash-file-sync.ps1").exists()

    linux = (REPO_ROOT / "templates" / "settings.json.linux.template").read_text(
        encoding="utf-8"
    )
    windows = (REPO_ROOT / "templates" / "settings.json.windows.template").read_text(
        encoding="utf-8"
    )
    for name, text, needle in (
        # Anchored at the project root since v0.2.97 (a relative hook path
        # fails once the session's cwd moves).
        ("linux", linux, 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/post-bash-file-sync.sh"'),
        ("windows", windows, '"${CLAUDE_PROJECT_DIR}/.claude/hooks/post-bash-file-sync.ps1"'),
    ):
        doc = json.loads(text)
        entries = [
            hook
            for group in doc["hooks"]["PostToolUse"]
            if group.get("matcher") == "Bash"
            for hook in group.get("hooks", [])
            if needle in hook.get("command", "")
        ]
        assert len(entries) == 1, (
            f"{name}: post-bash-file-sync must be registered exactly once on "
            f"PostToolUse(Bash); found {len(entries)}"
        )
        # NOT async: the hook emits an additionalContext envelope (the pending
        # duplicate-scan report), which an async hook's stdout cannot deliver.
        assert not entries[0].get("async"), f"{name}: entry must stay synchronous"


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_a_heredoc_into_knowledge_syncs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    node = project / "knowledge" / "node.md"
    node.write_text("# node\n", encoding="utf-8")

    result = _run_hook(project, "cat > knowledge/node.md <<'EOF'\n# node\nEOF")
    assert result.returncode == 0, result.stderr

    out = _await(marker)
    assert "KG-SYNC-RAN: knowledge/node.md" in out, out


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_a_heredoc_into_tmp_does_not_sync(tmp_path: Path) -> None:
    """The negative half of the same branch."""
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    outside = tmp_path / "scratch.md"
    outside.write_text("x", encoding="utf-8")

    result = _run_hook(project, f"cat > {outside} <<'EOF'\nx\nEOF")
    assert result.returncode == 0, result.stderr
    time.sleep(2)
    assert not marker.exists(), marker.read_text(encoding="utf-8")


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_sed_i_on_a_docs_file_routes_to_the_development_collection(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    doc = project / "docs" / "page.md"
    doc.write_text("a\n", encoding="utf-8")

    result = _run_hook(project, "sed -i 's/a/b/' docs/page.md")
    assert result.returncode == 0, result.stderr

    out = _await(marker)
    assert "KG-SYNC-RAN: docs/page.md" in out, out


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_sed_i_on_a_code_file_queues_the_drain_and_never_calls_kg_sync(
    tmp_path: Path,
) -> None:
    """A code file is a code-graph concern, NOT a KG-sync concern."""
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    module = project / "vco_lib" / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")

    result = _run_hook(project, "sed -i 's/1/2/' vco_lib/mod.py", session="s-code")
    assert result.returncode == 0, result.stderr

    queue = project / ".claude" / "state" / "codegraph_drain_s-code.txt"
    assert queue.exists(), result.stderr
    assert str(module) in queue.read_text(encoding="utf-8")
    time.sleep(1)
    assert not marker.exists(), "a .py file must not reach kg-sync"


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_a_command_that_wrote_nothing_spawns_nothing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)

    result = _run_hook(project, "git status --short 2>&1 | head -20")
    assert result.returncode == 0, result.stderr
    time.sleep(1)
    assert not marker.exists()
    assert not (project / ".claude" / "state" / "bash_write_scan.ts").exists()


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
@pytest.mark.parametrize("command", READ_ONLY_MENTIONS)
def test_a_read_only_command_reaches_neither_python_nor_kg_sync(
    tmp_path: Path, command: str
) -> None:
    """End to end: the real hook, a real read-only command, nothing spawned.

    A knowledge node and a docs page are written FIRST and their mtimes are
    fresh, so a scan — if one ran — would find both and route them. The
    watermark file is the tell: `bash_write_scan.ts` is written only by a
    scan that actually happened, so its absence proves the scan did not run,
    not merely that it found nothing.
    """
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    (project / "knowledge" / "notes.md").write_text("# n\n", encoding="utf-8")
    (project / "docs" / "x.md").write_text("# d\n", encoding="utf-8")
    (project / "docs" / "README.md").write_text("# r\n", encoding="utf-8")
    (project / "knowledge" / "a.md").write_text("# a\n", encoding="utf-8")
    (project / "knowledge" / "concepts").mkdir()

    result = _run_hook(project, command)
    assert result.returncode == 0, result.stderr
    time.sleep(1)
    assert not marker.exists(), f"{command!r} triggered kg-sync"
    assert not (project / ".claude" / "state" / "bash_write_scan.ts").exists(), (
        f"{command!r} ran the fallback scan"
    )


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_the_two_prefilters_accept_the_same_commands(tmp_path: Path) -> None:
    """`_lib/bash-write-targets.ps1` CLAIMS this test exists — so it does.

    Its header says "Test-VcoWriteSuspicious and vco_bash_write_prefilter
    must accept the same commands, which tests/test_v0295_bash_write_sync.py
    pins", and until v0.2.95 nothing did. A prefilter that disagrees across
    operating systems means a native-Windows user silently gets a different
    set of writes synced — the exact gap this lane closed for the Bash tool.

    Both implementations are driven as they ship: the bash one sourced and
    called, the PowerShell one dot-sourced and called.
    """
    corpus = READ_ONLY_MENTIONS + OPAQUE_WRITES + [
        "cat > knowledge/foo.md <<'EOF'\n# n\nEOF",
        "sed -i 's/a/b/' docs/architecture.md",
        "cp scratch/module.py vco_lib/module.py",
        "tee docs/out.md < in.md",
        "ls -la",
        "g" "it status --short 2>&1 | head -20",
        "pytest tests/ -q",
        "echo hi > /tmp/x.txt",
        "curl -s http://x/ 2>/dev/null",
        "touch knowledge/new.md",
        "grep -rn foo vco_lib/",
    ]
    payload = "\u0000".join(corpus)

    bash = shutil.which("bash")
    assert bash is not None
    script = (
        f'. "{LIB_SRC}/bash-write-targets.sh"\n'
        'while IFS= read -r -d "" c; do\n'
        '  if vco_bash_write_prefilter "$c"; then echo 1; else echo 0; fi\n'
        'done\n'
    )
    sh_out = subprocess.run(
        [bash, "-c", script],
        input=payload.replace("\u0000", "\x00") + "\x00",
        capture_output=True,
        text=True,
        env=child_env(),
        timeout=60,
    )
    assert sh_out.returncode == 0, sh_out.stderr
    sh_verdicts = sh_out.stdout.split()
    assert len(sh_verdicts) == len(corpus), sh_out.stdout

    exe = _powershell()
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this cross-OS gate cannot run and "
            "must not be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")

    ps_script = tmp_path / "parity.ps1"
    cases = tmp_path / "cases.txt"
    cases.write_text(payload, encoding="utf-8")
    ps_script.write_text(
        f'. "{LIB_SRC}/bash-write-targets.ps1"\n'
        f'$raw = [System.IO.File]::ReadAllText("{cases}")\n'
        "foreach ($c in $raw -split [char]0) {\n"
        "  if (Test-VcoWriteSuspicious $c) { Write-Output 1 } else { Write-Output 0 }\n"
        "}\n",
        encoding="utf-8",
    )
    ps_out = subprocess.run(
        [exe, "-NoProfile", "-File", str(ps_script)],
        capture_output=True,
        text=True,
        env=child_env(),
        timeout=120,
    )
    assert ps_out.returncode == 0, ps_out.stderr
    ps_verdicts = ps_out.stdout.split()
    assert len(ps_verdicts) == len(corpus), ps_out.stdout

    disagreements = [
        (cmd, sh, ps)
        for cmd, sh, ps in zip(corpus, sh_verdicts, ps_verdicts)
        if sh != ps
    ]
    assert not disagreements, (
        "the two prefilters disagree (command, bash, powershell):\n"
        + "\n".join(f"  {c!r}: sh={a} ps={b}" for c, a, b in disagreements)
    )
    # Positive control: the corpus must contain both verdicts, or "they
    # agree" would be satisfied by a filter that says no to everything.
    assert set(sh_verdicts) == {"0", "1"}, sh_verdicts


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_vct_disable_hooks_suppresses_everything(tmp_path: Path) -> None:
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    node = project / "knowledge" / "node.md"
    node.write_text("# node\n", encoding="utf-8")

    result = _run_hook(
        project,
        "cat > knowledge/node.md <<'EOF'\n# node\nEOF",
        extra_env={"VCT_DISABLE_HOOKS": "1"},
    )
    assert result.returncode == 0, result.stderr
    time.sleep(1)
    assert not marker.exists()


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_an_opaque_python_write_into_knowledge_is_recovered_by_the_scan(
    tmp_path: Path,
) -> None:
    """`python - <<EOF … open(p,'w') … EOF` is unparseable BY DESIGN.

    The bounded knowledge/+docs mtime fallback is what makes it sync anyway.
    """
    project = _project(tmp_path)
    marker = tmp_path / "kg.log"
    _stub_kg_sync(project, marker)
    node = project / "knowledge" / "opaque.md"
    node.write_text("# written by python\n", encoding="utf-8")

    command = (
        "python - <<'EOF'\n"
        "open('knowledge/opaque.md', 'w').write('# written by python\\n')\n"
        "EOF"
    )
    result = _run_hook(project, command)
    assert result.returncode == 0, result.stderr

    out = _await(marker)
    assert "KG-SYNC-RAN: knowledge/opaque.md" in out, out
    # …and the watermark advanced, so the next scan does not re-sync it.
    assert (project / ".claude" / "state" / "bash_write_scan.ts").exists()


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_a_cli_written_node_re_dispatches_the_summary_hook(tmp_path: Path) -> None:
    """kg-summary-generator is registered on Edit|Write(knowledge/**) only.

    Without the re-dispatch a CLI-written node has no entry in
    knowledge/.node_formats.json and renders EMPTY at hybrid_search's
    `summary` detail tier — the write syncs but retrieves worse than one made
    with Write. The existing hook is re-dispatched, not re-implemented, so it
    keeps its own path validation and 60 s debounce.
    """
    project = _project(tmp_path)
    _stub_kg_sync(project, tmp_path / "kg.log")
    summary_log = tmp_path / "summary-stdin.txt"
    stub = project / ".claude" / "hooks" / "kg-summary-generator.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n" f'cat >> "{summary_log}"\n' "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    node = project / "knowledge" / "summarised.md"
    node.write_text("# node\n", encoding="utf-8")

    result = _run_hook(project, "cat > knowledge/summarised.md <<'EOF'\n# node\nEOF")
    assert result.returncode == 0, result.stderr

    payload = _await(summary_log)
    assert str(node) in payload, payload
    assert '"tool_name"' in payload, payload


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_a_cli_written_docs_page_does_not_re_dispatch_the_summary_hook(
    tmp_path: Path,
) -> None:
    """The negative half: the summary sidecar is a knowledge/ concern."""
    project = _project(tmp_path)
    _stub_kg_sync(project, tmp_path / "kg.log")
    summary_log = tmp_path / "summary-stdin.txt"
    stub = project / ".claude" / "hooks" / "kg-summary-generator.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n" f'cat >> "{summary_log}"\n' "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    (project / "docs" / "page.md").write_text("a\n", encoding="utf-8")

    result = _run_hook(project, "sed -i 's/a/b/' docs/page.md")
    assert result.returncode == 0, result.stderr
    time.sleep(1.5)
    assert not summary_log.exists(), summary_log.read_text(encoding="utf-8")


@pytest.mark.skipif(not _have_bash(), reason="bash unavailable")
def test_the_hook_prints_nothing_on_plain_stdout_when_it_routes(
    tmp_path: Path,
) -> None:
    """PostToolUse stdout is discarded; anything printed there is noise that
    would break a JSON-envelope consumer."""
    project = _project(tmp_path)
    _stub_kg_sync(project, tmp_path / "kg.log")
    (project / "knowledge" / "n.md").write_text("x", encoding="utf-8")

    result = _run_hook(project, "cat > knowledge/n.md <<'EOF'\nx\nEOF")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout
