# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.94 containment: the suite must not rewrite the checkout's CLAUDE.md.

The leak (found while adding the ruff gate; it is the stray ``M CLAUDE.md``
that shows up in ``git status`` while a suite run is in flight):

  1. ``EmbeddingService.for_project()`` finishes by reconciling the deferral
     ledger of the root it resolved — ``_clear_failure_deferral`` on success,
     ``_write_failure_deferral`` on failure.
  2. Reconciling a ledger REWRITES ``<root>/CLAUDE.md``: entries present ⇒ the
     ``vco-deferral-reminder`` block is spliced in; none ⇒ it is stripped.
  3. ``_detect_project_root`` falls back to ``Path.cwd()`` when that directory
     holds a ``.claude/`` — and pytest's cwd is this checkout, whose
     ``CLAUDE.md`` is TRACKED.

``tests/conftest.py`` had a session-scoped guard that RESTORED the file at
session end, which is why a completed run looked clean; an interrupted run, or
another agent reading ``git status`` mid-run, saw the damage. v0.2.94 added the
PREVENTION beside it (``_rootless_embedding_root_never_resolves_the_checkout``)
``tests/common/child_env.py``'s ``KG_BASE_DIR`` pin for children (which
``_detect_project_root`` consults first, so the import pin stays untouched), and
per-call ``$VCT_ORCHESTRATOR_ROOT`` overrides in the two files that spawn
``analyze_code_graph.py`` — it hands its root to ``for_project()`` explicitly,
which outranks both env vars.

Why one chokepoint rather than a list of offending files: the strip is
IDEMPOTENT, so in any given run only the FIRST offender is observable. Fixing
"the file the watcher named" would have had to be repeated an unknown number of
times and could never be shown complete.

These tests pin both halves so the fix cannot rot silently — the reconcile
really does rewrite the CLAUDE.md of the root it is handed (without that
positive control, "nothing changed" would pass against a no-op), and a rootless
resolution never lands on this checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import vco_lib.embedding_service as embedding_service_mod  # noqa: E402
from vco_lib.deferral_report import _REMINDER_BEGIN, _REMINDER_END  # noqa: E402
from vco_lib.embedding_providers.codeembed import CodeEmbedAdapter  # noqa: E402
from vco_lib.embedding_providers.ollama import OllamaAdapter  # noqa: E402
from vco_lib.embedding_providers.openai import (  # noqa: E402
    OpenAIAdapter,
    ValidationResult,
)

REPO_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

#: The artefacts a deferral-ledger reconcile leaves in the root it resolved.
#: Every one of them landing in THIS checkout is the field shape of the leak.
LEDGER_ARTEFACTS = (
    "EMBEDDING_FAILURES.md",
    "UPDATE_DEFERRED.md",
    "UPDATE_DEFERRED.json",
)

#: The UNPATCHED resolver. Captured at import — collection runs before any
#: fixture, so this is the real function even though conftest's session-scoped
#: containment replaces the module attribute for the duration of the run. It is
#: how the test below can state what production does without doing it.
_REAL_DETECT = embedding_service_mod._detect_project_root

_REMINDER = (
    f"{_REMINDER_BEGIN}\n"
    "**Pending VCO action**: sentinel block for the containment test.\n"
    f"{_REMINDER_END}\n"
)


def _repo_claude_md_sha() -> str:
    """Hash of the checkout's tracked CLAUDE.md (``MISSING`` when absent)."""
    try:
        return hashlib.sha256(REPO_CLAUDE_MD.read_bytes()).hexdigest()
    except OSError:
        return "MISSING"


def _ledger_state(root: Path) -> dict[str, str]:
    """Hash of each ledger artefact under ``root/.claude/context`` (or MISSING)."""
    ctx = root / ".claude" / "context"
    out: dict[str, str] = {}
    for name in LEDGER_ARTEFACTS:
        try:
            out[name] = hashlib.sha256((ctx / name).read_bytes()).hexdigest()
        except OSError:
            out[name] = "MISSING"
    return out


def _seed_sentinel_root(tmp_path: Path) -> Path:
    """A throwaway project root carrying a reminder block, like a real one."""
    root = tmp_path / "sentinel_root"
    (root / ".claude" / "context").mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(
        _REMINDER + "\n# Sentinel project\n", encoding="utf-8"
    )
    return root


def test_ledger_reconcile_rewrites_only_the_root_it_is_given(tmp_path):
    """Positive control + containment in one call.

    ``_clear_failure_deferral(root)`` MUST strip the reminder from
    ``root/CLAUDE.md`` — without that half, the "checkout untouched" assertion
    would pass even against a no-op — and MUST leave every other CLAUDE.md on
    the machine, this checkout's included, byte-identical.
    """
    root = _seed_sentinel_root(tmp_path)
    before_repo = _repo_claude_md_sha()
    assert before_repo != "MISSING", "the checkout's CLAUDE.md should exist"

    embedding_service_mod._clear_failure_deferral(root)

    assert _repo_claude_md_sha() == before_repo, (
        "reconciling a deferral ledger under a tmp root modified the "
        f"CHECKOUT's tracked {REPO_CLAUDE_MD} — a write escaped its root."
    )

    sentinel_text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert _REMINDER_BEGIN not in sentinel_text, (
        "the reconcile did NOT rewrite the sentinel root's CLAUDE.md — the "
        "positive control failed, so the containment assertion above proves "
        "nothing. Did _clear_failure_deferral stop reconciling, or start "
        "soft-failing on this input?"
    )
    assert "# Sentinel project" in sentinel_text, "body outside the block lost"


def test_unpatched_resolver_really_would_pick_this_checkout(monkeypatch):
    """Name the hazard executably, using the pre-patch resolver.

    This is why the containment exists. If this ever stops being true — say
    ``_detect_project_root`` grows a "never the orchestrator root" rule of its
    own — the conftest fixture can be simplified; read this test first.
    """
    for var in ("KG_BASE_DIR", "VCT_ORCHESTRATOR_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(REPO_ROOT)

    assert (REPO_ROOT / ".claude").is_dir(), "premise: checkout has .claude/"
    assert _REAL_DETECT() == REPO_ROOT.resolve(), (
        "a rootless EmbeddingService.for_project() resolves THIS checkout, so "
        "its ledger reconcile writes the tracked CLAUDE.md."
    )


def test_suite_wide_containment_reroutes_rootless_resolution(tmp_path, monkeypatch):
    """The chokepoint: as the SUITE sees it, rootless never means the checkout.

    Delete conftest's ``_rootless_embedding_root_never_resolves_the_checkout``
    and this goes red — that is the regression it exists to catch.
    """
    for var in ("KG_BASE_DIR", "VCT_ORCHESTRATOR_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(REPO_ROOT)  # the hostile cwd, on purpose

    resolved = embedding_service_mod._detect_project_root()
    assert resolved != REPO_ROOT.resolve(), (
        "a rootless project-root resolution still lands on this checkout, so "
        "any for_project() in the suite will rewrite the tracked CLAUDE.md."
    )

    # …and an EXPLICIT root is still resolved for real, not swallowed.
    explicit = tmp_path / "explicit_root"
    explicit.mkdir()
    assert embedding_service_mod._detect_project_root(explicit) == explicit.resolve()


def test_successful_for_project_leaves_the_checkout_alone(tmp_path, monkeypatch):
    """End-to-end on the leaking path, relying only on the suite-wide fix.

    A SUCCESSFUL construction is the leaking case (it calls
    ``_clear_failure_deferral``). No local patching of the resolver here, on
    purpose: this measures what an ordinary test in this suite does.
    """
    for var in ("KG_BASE_DIR", "VCT_ORCHESTRATOR_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(REPO_ROOT)
    monkeypatch.setenv("VCT_STATE_DIR", str(tmp_path / ".vct"))

    before_repo = _repo_claude_md_sha()

    ollama = MagicMock(spec=OllamaAdapter)
    ollama.is_reachable.return_value = True
    ollama.list_embedding_models.return_value = ["qwen3-embedding:0.6b"]
    code = MagicMock(spec=CodeEmbedAdapter)
    code.is_reachable.return_value = True
    openai = MagicMock(spec=OpenAIAdapter)
    openai.validate.return_value = ValidationResult(valid=False, reason="no key")

    with patch("vco_lib.embedding_service.OllamaAdapter", return_value=ollama), \
         patch("vco_lib.embedding_service.CodeEmbedAdapter", return_value=code), \
         patch("vco_lib.embedding_service.OpenAIAdapter", return_value=openai):
        embedding_service_mod.EmbeddingService.for_project()

    assert _repo_claude_md_sha() == before_repo, (
        f"EmbeddingService.for_project() modified the tracked {REPO_CLAUDE_MD}. "
        "conftest's session-end restore would hide this from a later `git "
        "status`, so this assertion is the only place the IN-PROCESS write is "
        "observable — keep preventing it here rather than leaning on repair."
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX hook; the .ps1 sibling is Windows'")
@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash on PATH")
def test_real_post_edit_hook_reconciles_its_own_project_not_the_checkout(tmp_path):
    """The FIELD half: run the shipped hook for real, from a hostile cwd.

    Everything above contains the leak for the SUITE — an in-process patch
    (conftest) and an env pin for children built through
    ``tests/common/child_env.py``. Neither reaches a user's machine, and the
    same mechanism runs there: ``templates/hooks/post-edit-outcome.sh`` spawns
    a Python child that calls ``emit_outcome_event``, and downstream of it
    ``EmbeddingService.for_project()`` is reached with no root, so
    ``_detect_project_root`` falls to ``Path.cwd()`` — whatever directory the
    harness handed the hook, which is not guaranteed to be the project. It
    then writes THAT root's ledger (``EMBEDDING_FAILURES.md`` /
    ``UPDATE_DEFERRED.*``) and rewrites its ``CLAUDE.md``.

    So this runs the real hook from a fixture project under ``tmp_path``, with
    cwd deliberately set to this checkout and a from-scratch environment
    carrying neither ``KG_BASE_DIR`` nor ``$VCT_ORCHESTRATOR_ROOT`` — exactly
    the conditions under which the child used to pick the checkout.

    **Do not route this env through ``child_env()``.** That helper pins
    ``KG_BASE_DIR`` itself, which is the very thing under test: the assertions
    would then hold with the hook's pin deleted, and the test would prove
    nothing. The scrubbing is the point.

    Both halves are asserted, because either alone is satisfiable by an
    accident: the fixture project MUST receive the ledger (otherwise the
    emit chain soft-failed before reaching ``for_project()`` and "the checkout
    is untouched" is vacuous), and the checkout's ``CLAUDE.md`` + ledger MUST
    be byte-identical.

    Red-proof (2026-09-09): with the ``KG_BASE_DIR`` pin removed from
    ``post-edit-outcome.sh``, the fixture receives nothing and the checkout's
    ``.claude/context/`` gains ``EMBEDDING_FAILURES.md`` + ``UPDATE_DEFERRED.*``.
    Note which assertion caught it: the checkout's ``CLAUDE.md`` did NOT change
    in that run, because its reminder block already matched what the reconcile
    would have written. The ledger-artefact comparison is what makes the escape
    observable; the CLAUDE.md hash alone would have passed.
    """
    hook_src = REPO_ROOT / "templates" / "hooks" / "post-edit-outcome.sh"

    project = tmp_path / "fixture_project"
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (project / ".claude" / "context").mkdir(parents=True)
    # Installed layout: <project>/.claude/hooks/, so the hook's own
    # `$SCRIPT_DIR/../..` fallback resolves the fixture project — the branch
    # that matters, since CLAUDE_PROJECT_DIR is deliberately not set below.
    shutil.copy2(hook_src, hooks / hook_src.name)
    shutil.copytree(hook_src.parent / "_lib", hooks / "_lib")
    (project / "CLAUDE.md").write_text("# fixture project\n", encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        # The child imports vco_lib + claude_mcp_servers from THIS tree.
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'claude_mcp_servers'}",
        # Tier-1 of _lib/resolve-vco-venv.sh accepts an interpreter path.
        "VCT_VENV": sys.executable,
        "VCT_STATE_DIR": str(tmp_path / "state"),
        # Dead ports: no embedding backend is reachable, so for_project()
        # takes its FAILURE path — which reconciles the ledger just as the
        # success path does, without depending on a live Ollama in CI.
        "OLLAMA_URL": "http://127.0.0.1:9",
        "CODE_EMBED_SERVICE_URL": "http://127.0.0.1:9",
        "WEAVIATE_URL": "http://127.0.0.1:9",
    }

    before_claude_md = _repo_claude_md_sha()
    before_ledger = _ledger_state(REPO_ROOT)

    payload = {
        "tool_name": "Edit",
        "session_id": "v0294-containment",
        "tool_input": {
            "file_path": str(project / "edited.py"),
            "old_string": "a",
            "new_string": "ab",
        },
    }
    proc = subprocess.run(
        ["bash", str(hooks / hook_src.name)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),  # the hostile cwd, on purpose
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"the hook must never fail the host tool call: rc={proc.returncode} "
        f"stderr={proc.stderr[-2000:]}"
    )

    fixture_ledger = _ledger_state(project)
    assert fixture_ledger["EMBEDDING_FAILURES.md"] != "MISSING", (
        "the hook's child never reached EmbeddingService.for_project() with "
        "the fixture project as its root, so the containment assertions below "
        "prove nothing. Either the emit chain soft-failed earlier (check "
        f"stderr: {proc.stderr[-2000:]}) or the KG_BASE_DIR pin in "
        f"{hook_src} stopped taking effect."
    )
    assert fixture_ledger["UPDATE_DEFERRED.md"] != "MISSING", (
        "the failure deferral was not written into the fixture project"
    )

    assert _repo_claude_md_sha() == before_claude_md, (
        f"the shipped hook rewrote the tracked {REPO_CLAUDE_MD} — its child "
        "resolved a project root of its own instead of the one the hook "
        "already knew."
    )
    assert _ledger_state(REPO_ROOT) == before_ledger, (
        f"the shipped hook wrote deferral-ledger artefacts into {REPO_ROOT}"
        "/.claude/context/ instead of the project it was fired for. In the "
        "field this is a user's UNRELATED project getting another project's "
        "embedding-failure ledger."
    )
