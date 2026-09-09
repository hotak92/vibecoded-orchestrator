# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""One venv ladder, one gate, both OSes (v0.2.94).

The cross-OS divergence this pins, as found:

* ``kg-duplicates`` (bash) had NO ladder at all — it sourced
  ``$PROJECT_ROOT/.venv`` when one happened to exist and otherwise ran
  whatever ``python`` was first on PATH, while ``kg-duplicates.ps1`` carried
  the full orchestrator-venv ladder. Same tool, two different answers to
  "which interpreter runs this?", decided by the user's OS.
* ``kg-duplicates.ps1``'s gate named only ``weaviate``, WEAKER than what
  ``detect_duplicates.py`` requires (it needs ``vco_lib`` to resolve the
  named-vector slot at all, v0.2.94) — a gate weaker than its script is a
  gate that lets the failure happen one layer deeper, where the error names
  the wrong problem.
* ``kg-sync`` and ``kg-dedup`` each carried a verbatim copy of the ladder
  (bash AND PowerShell): four copies of one decision, which is how the
  divergence above survived unnoticed.

The ladder now has ONE home per flavour (``templates/scripts/vct_venv_ladder.sh``
/ ``.ps1``) and every wrapper declares only what is genuinely its own: tool
name, import probe, exit code, refusal tail.

The gate-equality checks below are LINE-ANCHORED: they locate each wrapper's
single declaration line and compare the values, so a drift is reported with
the file and line that caused it rather than as "some token is missing".
The refusal behaviour itself is DRIVEN through the real wrappers — a source
scan cannot tell a refusal that fires from one that is merely spelled.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.wrapper_staging import (
    LADDER_PS1,
    LADDER_SH,
    stage_scripts,
)

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "templates" / "scripts"

#: Every wrapper that must resolve its interpreter through the shared ladder,
#: with the module list its own script genuinely requires.
#:
#: The probe is NOT uniform on purpose. Each entry names what that script needs
#: to reach a CORRECT VERDICT (the rule stated in `vct_venv_ladder.sh`), read
#: from the script — module-scope imports, `try:`-guarded module-scope imports
#: that print and CONTINUE, and function-local imports whose absence changes
#: the ANSWER rather than the speed:
#:   * `search_knowledge.py` / `get_node_info.py` — `weaviate` only; their
#:     vco_lib / weaviate_mcp imports are function-local and guarded, with a
#:     documented legacy fallback. A gate STRICTER than its script would
#:     reject venvs that could have run it.
#:   * `migrate_to_vocabulary.py` — imports `sync_knowledge_graph` at module
#:     scope, which hard-imports weaviate_mcp.chunking + several vco_lib
#:     modules. Three modules.
#:   * `query_code_graph.py` / `analyze_code_graph.py` — module-scope
#:     `vco_lib.*` imports since v0.2.75.
#:   * `sync_knowledge_graph.py` — `weaviate_mcp.chunking` (v0.2.49 Bug K).
GATED_WRAPPERS = {
    # tool           bash            ps1                    modules
    "kg-sync": (
        "kg-sync",
        "kg-sync.ps1",
        "import weaviate, weaviate_mcp, vco_lib",
    ),
    "kg-dedup": ("kg-dedup", "kg-dedup.ps1", "import weaviate, vco_lib"),
    "kg-duplicates": (
        "kg-duplicates",
        "kg-duplicates.ps1",
        "import weaviate, vco_lib",
    ),
    "kg-search": ("kg-search", "kg-search.ps1", "import weaviate, vco_lib"),
    "kg-info": ("kg-info", "kg-info.ps1", "import weaviate, vco_lib"),
    "kg-migrate": (
        "kg-migrate",
        "kg-migrate.ps1",
        "import weaviate, weaviate_mcp, vco_lib",
    ),
    "code-graph-query": (
        "code-graph-query",
        "code-graph-query.ps1",
        "import weaviate, weaviate_mcp, vco_lib",
    ),
    "code-graph-analyze": (
        "code-graph-analyze",
        "code-graph-analyze.ps1",
        "import weaviate, weaviate_mcp, vco_lib",
    ),
}

#: Every wrapper pair must be covered — a new one that quietly ships its own
#: ladder is the defect this file exists to prevent.
EXPECTED_PAIR_COUNT = 8

_BASH_DECL = re.compile(r'^LADDER_IMPORT="([^"]*)"\s*$')
_PS1_DECL = re.compile(r'^\$LadderImport\s*=\s*"([^"]*)"\s*$')


def _declared_probe(path: Path, pattern: re.Pattern, encoding: str) -> tuple[str, int]:
    """Return (probe, 1-based line number) of the wrapper's ONE declaration."""
    hits = [
        (m.group(1), n)
        for n, line in enumerate(path.read_text(encoding=encoding).splitlines(), 1)
        if (m := pattern.match(line))
    ]
    assert len(hits) == 1, (
        f"{path.name}: expected exactly ONE import-probe declaration, found "
        f"{len(hits)} at lines {[n for _, n in hits]}. The probe is what the "
        f"ladder executes; two declarations mean one of them is a lie."
    )
    return hits[0]


@pytest.mark.parametrize("tool", sorted(GATED_WRAPPERS))
def test_both_flavours_gate_on_the_same_modules(tool: str) -> None:
    """Line-anchored: the bash and PowerShell gates name the SAME modules.

    Red-proof: change either declaration alone (e.g. drop ``vco_lib`` from
    ``kg-duplicates.ps1``, which is the state this release found) and this
    fails naming both files and both line numbers.
    """
    sh_name, ps1_name, expected = GATED_WRAPPERS[tool]
    sh_probe, sh_line = _declared_probe(SCRIPTS / sh_name, _BASH_DECL, "utf-8")
    ps1_probe, ps1_line = _declared_probe(
        SCRIPTS / ps1_name, _PS1_DECL, "utf-8-sig"
    )

    assert sh_probe == ps1_probe, (
        f"cross-OS gate divergence: {sh_name}:{sh_line} gates on "
        f"{sh_probe!r} but {ps1_name}:{ps1_line} gates on {ps1_probe!r}. "
        f"Same tool, same requirement, both OSes."
    )
    assert sh_probe == expected, (
        f"{sh_name}:{sh_line} gates on {sh_probe!r}; its script requires "
        f"{expected!r}. A gate weaker than its script defers the failure to "
        f"a layer that names the wrong problem."
    )


@pytest.mark.parametrize("tool", sorted(GATED_WRAPPERS))
def test_neither_flavour_reinlines_the_ladder(tool: str) -> None:
    """The ladder is sourced, never re-inlined — that is the whole point.

    Pins the specific shapes that WERE duplicated, by name, so a future
    "just paste it back in" is caught rather than silently doubling the
    number of places the tier order lives.
    """
    sh_name, ps1_name, _ = GATED_WRAPPERS[tool]
    sh = (SCRIPTS / sh_name).read_text(encoding="utf-8")
    ps1 = (SCRIPTS / ps1_name).read_text(encoding="utf-8-sig")

    assert "vct_venv_ladder.sh" in sh, f"{sh_name} must source the shared ladder"
    assert "vct_venv_ladder.ps1" in ps1, f"{ps1_name} must dot-source the shared ladder"

    for inlined in ("venv_has_kg_deps()", "venv_has_dedup_deps()",
                    "venv_has_code_graph_deps()", "venv_has_analyzer_deps()",
                    "_interp_for_candidate()",
                    "orchestrator_root_from_project_env()",
                    "is_vco_orchestrator_clone()",
                    "CANDIDATES=(",
                    "source \"$VENV_PATH/bin/activate\""):
        assert inlined not in sh, f"{sh_name} re-inlines `{inlined}` — one home"
    for inlined in ("function Test-VenvHasKgDeps", "function Test-VenvHasDedupDeps",
                    "function Test-VenvHasCodeGraphDeps", "function Test-AnalyzerDeps",
                    "function Resolve-Interp",
                    "function Get-OrchestratorRootFromProjectEnv",
                    "function Test-VcoOrchestratorClone",
                    "function Get-VenvPythonCandidates",
                    "$Candidates = @("):
        assert inlined not in ps1, f"{ps1_name} re-inlines `{inlined}` — one home"


def test_every_ladder_sourcing_wrapper_is_covered_here() -> None:
    """No wrapper may source the ladder without being pinned by this file.

    Discovered from the tree, not from a hand-kept list: a new wrapper that
    sources the ladder but is absent from ``GATED_WRAPPERS`` would keep its
    gate un-compared across OSes — the exact hole this release closed.
    """
    sourcing = {
        p.name
        for p in SCRIPTS.iterdir()
        if p.is_file()
        and p.name != LADDER_SH.name  # the ladder names itself in its docs
        and not p.name.endswith(".ps1")
        and not p.name.endswith(".py")
        and "vct_venv_ladder.sh" in p.read_text(encoding="utf-8", errors="ignore")
    }
    covered = {sh for sh, _ps1, _mods in GATED_WRAPPERS.values()}
    assert sourcing == covered, (
        f"bash wrappers sourcing the ladder but not pinned here: "
        f"{sorted(sourcing - covered)}; pinned but no longer sourcing: "
        f"{sorted(covered - sourcing)}"
    )
    assert len(GATED_WRAPPERS) == EXPECTED_PAIR_COUNT, (
        f"expected {EXPECTED_PAIR_COUNT} wrapper pairs, found "
        f"{len(GATED_WRAPPERS)} — update the count deliberately"
    )


#: The `set` line each bash wrapper shipped BEFORE v0.2.94. A refactor may not
#: change a file's failure posture as a side effect: `kg-duplicates` and
#: `kg-migrate` both had `set -e`, the rewrite dropped it, and "state
#: unchanged" stopped being true (v0.2.94 review item 5).
ORIGINAL_SET_LINES = {
    "kg-sync": None,
    "kg-dedup": None,
    "kg-duplicates": "set -e",
    "kg-search": None,
    "kg-info": None,
    "kg-migrate": "set -e",
    "code-graph-query": None,
    "code-graph-analyze": None,
}


@pytest.mark.parametrize("tool", sorted(GATED_WRAPPERS))
def test_each_wrapper_keeps_its_original_shell_options(tool: str) -> None:
    """`set -e` (or its absence) is behaviour, not formatting.

    Red-proof: delete the `set -e` line from `kg-duplicates` and this fails;
    ADD one to `kg-sync` and it fails too — a wrapper that did not have it
    must not silently acquire it either, since every `[ … ] && …` guard in its
    body then becomes a potential early exit.
    """
    sh_name, _ps1, _mods = GATED_WRAPPERS[tool]
    body = (SCRIPTS / sh_name).read_text(encoding="utf-8")
    found = [
        line.strip()
        for line in body.splitlines()
        if re.fullmatch(r"set -[a-zA-Z]+(\s+-[a-zA-Z]+)*", line.strip())
    ]
    expected = ORIGINAL_SET_LINES[tool]
    if expected is None:
        assert not found, (
            f"{sh_name} acquired shell options it never shipped with: {found}"
        )
    else:
        assert found == [expected], (
            f"{sh_name} shipped `{expected}` before v0.2.94; found {found}. "
            f"A refactor may not change a wrapper's failure posture."
        )


def test_the_shared_ladder_is_set_e_safe(tmp_path: Path) -> None:
    """DRIVEN: the ladder's `[ … ] && …` guards do not abort under `set -e`.

    `kg-duplicates` runs under `set -e`, so a guard whose test fails on the
    miss path (no `$VCT_VENV`, no `$VCT_INSTALL_ROOT`, …) would abort the
    wrapper BEFORE its refusal — turning a named diagnosis into a silent exit.
    Driving the real wrapper with every channel stripped exercises that path.
    """
    scripts = stage_scripts(tmp_path / ".claude" / "scripts", "kg-duplicates")
    proc = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=_stripped_env(), cwd=str(tmp_path),
        timeout=120,
    )
    assert "set -e" in (scripts / "kg-duplicates").read_text(encoding="utf-8")
    assert proc.returncode == 1, (
        f"under `set -e` the ladder must reach the refusal, not abort: "
        f"rc={proc.returncode} stderr={proc.stderr!r}"
    )
    assert "kg-duplicates: ERROR - no Python environment" in proc.stderr


def _fake_qualifying_venv(root: Path) -> Path:
    (root / ".venv" / "bin").mkdir(parents=True)
    py = root / ".venv" / "bin" / "python"
    py.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\necho "RAN=$1"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)
    return py


def test_the_env_orchestrator_root_tier_resolves_and_is_clone_validated(
    tmp_path: Path,
) -> None:
    """v0.2.94 review item 2a: `$VCT_ORCHESTRATOR_ROOT` from the ENVIRONMENT.

    The Python half of this ladder honours both install-root env vars; the
    shell half honoured only the file-backed one. Two halves of one ladder
    answering differently is the divergence class this release closes.

    Both directions are driven: a VALID clone resolves, and a path that is not
    a clone is REFUSED — an exported value survives a moved clone, so this is
    the tier that most needs validating.
    """
    clone = tmp_path / "orch"
    _fake_qualifying_venv(clone)
    (clone / "install.py").write_text("", encoding="utf-8")
    (clone / "first-install.sh").write_text("", encoding="utf-8")

    scripts = stage_scripts(tmp_path / ".claude" / "scripts", "kg-duplicates")
    env = {**_stripped_env(), "VCT_ORCHESTRATOR_ROOT": str(clone)}
    ok = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    assert ok.returncode == 0, f"{ok.stdout!r} {ok.stderr!r}"
    assert "detect_duplicates.py" in ok.stdout, ok.stdout

    not_a_clone = tmp_path / "notclone"
    _fake_qualifying_venv(not_a_clone)
    env2 = {**_stripped_env(), "VCT_ORCHESTRATOR_ROOT": str(not_a_clone)}
    refused = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=env2, cwd=str(tmp_path), timeout=120,
    )
    assert refused.returncode == 1, (
        "an exported VCT_ORCHESTRATOR_ROOT that is not a clone must NOT be "
        f"used: {refused.stdout!r} {refused.stderr!r}"
    )
    assert str(not_a_clone) not in refused.stderr, (
        "the rejected root must not even appear among the probed candidates"
    )


def test_every_refusal_ends_with_the_marker_line(tmp_path: Path) -> None:
    """v0.2.94 review item 3: the LAST refusal line carries ⚠️.

    `post-file-edit.sh` filters the scan's output on `✅|⚠️|📊|❌`, so a
    refusal with none of those markers produced an EMPTY match and the
    every-10-edits check became a silent no-op. Driven, because a source scan
    cannot tell a line that is printed from one that is merely spelled.
    """
    scripts = stage_scripts(tmp_path / ".claude" / "scripts", "kg-duplicates")
    proc = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=_stripped_env(), cwd=str(tmp_path),
        timeout=120,
    )
    assert proc.returncode == 1
    last = [ln for ln in proc.stderr.splitlines() if ln.strip()][-1]
    assert last.startswith("⚠"), (
        f"the refusal's last line must carry the ⚠️ marker every consumer "
        f"greps for; got {last!r}"
    )
    assert "did NOT run" in last and "kg-duplicates" in last


def test_the_ladder_ships_with_the_wrappers() -> None:
    """A wrapper whose ladder is not installed is a wrapper that cannot run.

    Both flavours must match `bundle_globs.script_patterns()`, which is what
    copies `templates/scripts/` into `.claude/scripts/`.
    """
    from vco_lib.bundle_globs import script_patterns

    patterns = script_patterns()
    for name in (LADDER_SH.name, LADDER_PS1.name):
        assert any(fnmatch.fnmatch(name, p) for p in patterns), (
            f"{name} would NOT be copied into .claude/scripts/ — every wrapper "
            f"that sources it would refuse on a real install"
        )


def test_the_ps1_ladder_keeps_its_bom() -> None:
    """Windows PowerShell needs the UTF-8 BOM on shipped .ps1 files."""
    assert LADDER_PS1.read_bytes().startswith(b"\xef\xbb\xbf")


def _bash() -> str:
    exe = shutil.which("bash")
    if exe is None:  # pragma: no cover - POSIX hosts always have bash
        pytest.skip("no bash on this machine")
    return exe


def _stripped_env() -> dict:
    """Every env channel the ladder could resolve through, removed.

    ``VCT_ORCHESTRATOR_ROOT`` joined the list in v0.2.94 (review item 2a): the
    shell ladder now honours it from the ENVIRONMENT, so a maintainer shell
    that exports a real clone would otherwise resolve a qualifying interpreter
    and this test would silently stop exercising the refusal. It caught exactly
    that the first time the tier landed.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "VCT_VENV",
            "VCT_INSTALL_ROOT",
            "VCT_ORCHESTRATOR_ROOT",
            "VIRTUAL_ENV",
        )
    }


@pytest.mark.parametrize(
    ("tool", "target_script", "expected_exit"),
    [
        ("kg-duplicates", "detect_duplicates.py", 1),
        ("kg-dedup", None, 1),
        # v0.2.94 newcomers: these had NO refusal at all — they fell through
        # to a bare `python` / `python3`.
        ("code-graph-analyze", "analyze_code_graph.py", 1),
        ("kg-search", "search_knowledge.py", 1),
    ],
)
def test_bash_wrapper_refuses_instead_of_running_a_bare_interpreter(
    tmp_path: Path, tool: str, target_script: str | None, expected_exit: int
) -> None:
    """DRIVEN: with no qualifying venv, the wrapper refuses — it does not run.

    This is the behaviour `kg-duplicates` did NOT have: it fell through to
    whatever `python` was on PATH and died inside the script with a
    ModuleNotFoundError naming the wrong problem.

    The staged project is deliberately NOT a VCO clone and carries no
    `.claude/env`, so every tier misses and the candidate list is empty.
    """
    scripts = stage_scripts(tmp_path / ".claude" / "scripts", tool)
    if target_script:
        # A tripwire: if the wrapper ever runs the script anyway, we see it.
        (scripts / target_script).write_text(
            "raise SystemExit('THE SCRIPT MUST NOT RUN')\n", encoding="utf-8"
        )

    proc = subprocess.run(
        [_bash(), str(scripts / tool)],
        capture_output=True, text=True, env=_stripped_env(), cwd=str(tmp_path),
        timeout=120,
    )

    assert proc.returncode == expected_exit, (
        f"{tool} must refuse with exit {expected_exit}; got {proc.returncode}\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "THE SCRIPT MUST NOT RUN" not in (proc.stdout + proc.stderr), (
        f"{tool} invoked its script with an unqualified interpreter"
    )
    assert f"{tool}: ERROR - no Python environment" in proc.stderr, (
        f"the refusal must reach STDERR; stderr={proc.stderr!r}"
    )
    # The prose names EVERY module of that tool's gate, derived from the probe
    # itself — one, two and three-module gates all ship, so the sentence is
    # checked against the wrapper's own declaration rather than a fixed shape.
    mods = [
        m.strip()
        for m in GATED_WRAPPERS[tool][2].removeprefix("import ").split(",")
        if m.strip()
    ]
    for module in mods:
        assert f"'{module}'" in proc.stderr, (
            f"{tool}'s refusal must name `{module}` (its own gate); "
            f"stderr={proc.stderr!r}"
        )
    if len(mods) == 1:
        assert f"only when '{mods[0]}' imports from it." in proc.stderr
    else:
        assert f"'{mods[-1]}' import from it." in proc.stderr
    assert "Fix by any ONE of:" in proc.stderr
    assert proc.stdout.strip() == "", f"stdout must stay clean: {proc.stdout!r}"


def test_bash_wrapper_refuses_when_its_ladder_is_missing(tmp_path: Path) -> None:
    """A half-installed bundle refuses loudly; it never guesses an interpreter.

    Staging the wrapper WITHOUT the lib is exactly the stale-bundle shape (the
    wrapper updated, its new sibling not yet copied).
    """
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPTS / "kg-duplicates", scripts / "kg-duplicates")
    (scripts / "detect_duplicates.py").write_text(
        "raise SystemExit('THE SCRIPT MUST NOT RUN')\n", encoding="utf-8"
    )

    proc = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates")],
        capture_output=True, text=True, env=_stripped_env(), cwd=str(tmp_path),
        timeout=120,
    )

    assert proc.returncode == 1, proc.stderr
    assert "missing" in proc.stderr and "vct_venv_ladder.sh" in proc.stderr
    assert "THE SCRIPT MUST NOT RUN" not in (proc.stdout + proc.stderr)


def test_bash_wrapper_runs_the_script_through_a_qualifying_venv(
    tmp_path: Path,
) -> None:
    """The leave-alone half: a qualifying venv IS used, and VIRTUAL_ENV is set.

    Without this, "refuses everything" would pass every assertion above.
    """
    fake_venv = tmp_path / "fake-venv"
    (fake_venv / "bin").mkdir(parents=True)
    py = fake_venv / "bin" / "python"
    # Qualifies for any probe, and echoes what it was asked to run.
    py.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'echo "RAN=$1 VIRTUAL_ENV=$VIRTUAL_ENV"\n',
        encoding="utf-8",
    )
    py.chmod(0o755)

    scripts = stage_scripts(tmp_path / ".claude" / "scripts", "kg-duplicates")
    env = {**_stripped_env(), "VCT_VENV": str(fake_venv)}

    proc = subprocess.run(
        [_bash(), str(scripts / "kg-duplicates"), "--threshold", "0.9"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )

    assert proc.returncode == 0, f"{proc.stdout!r} {proc.stderr!r}"
    assert "detect_duplicates.py" in proc.stdout, proc.stdout
    assert f"VIRTUAL_ENV={fake_venv}" in proc.stdout, (
        f"the ladder must export VIRTUAL_ENV=<venv root>; got {proc.stdout!r}"
    )
