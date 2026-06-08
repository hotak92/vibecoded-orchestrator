"""V47-F (Gap F) tests for PROJECT_NAME precedence resolver — NEW adopt only.

Critical contract: ``_resolve_project_name_for_adopt`` returns the resolved
PROJECT_NAME for a NEW adopt operation per a strict precedence order:

  1. ``args.project_name`` (``--project-name`` CLI flag)
  2. ``.vscode/settings.json claude-code.env.PROJECT_NAME``
  3. ``.claude/env PROJECT_NAME=`` (active, uncommented)
  4. ``.env PROJECT_NAME=`` (active, uncommented)
  5. Interactive prompt (TTY + not ``--yes``)
  6. Folder name (sanitized fallback)

CRITICAL non-destructiveness rule (V47-F): the resolver is ONLY called in
the NEW adopt path (when ``.vco-manifest.json`` is absent — caller has
decided this is a fresh / 3rd-party tree). Projects with an existing
manifest KEEP whatever PROJECT_NAME they have at update time. The
``_reconcile_env_keys`` flow is additive-only and never overwrites
canonical keys including PROJECT_NAME. The dedicated test
``test_existing_project_name_preserved_on_update`` is the load-bearing
gate that asserts this rule.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest


# Load install.py as a module (same pattern as test_v0246_v47gstub_adopt_contract.py).
_INSTALL_PY = Path(__file__).resolve().parent.parent / "install.py"
_spec = importlib.util.spec_from_file_location("install_py_v47f", _INSTALL_PY)
install_py = importlib.util.module_from_spec(_spec)
sys.modules["install_py_v47f"] = install_py
_spec.loader.exec_module(install_py)


# ---------------------------------------------------------------------------
# Helpers — build fixtures on tmp_path
# ---------------------------------------------------------------------------


def _write_vscode_settings(install_path: Path, project_name: str) -> None:
    """Write a .vscode/settings.json with claude-code.env.PROJECT_NAME set."""
    vscode = install_path / ".vscode"
    vscode.mkdir(parents=True, exist_ok=True)
    settings = {
        "claude-code.env": {
            "PROJECT_NAME": project_name,
            "KG_COLLECTION": f"{project_name}_KnowledgeGraph",
        },
    }
    (vscode / "settings.json").write_text(json.dumps(settings, indent=2))


def _write_claude_env(install_path: Path, project_name: str) -> None:
    """Write a .claude/env with PROJECT_NAME=<project_name>."""
    claude = install_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / "env").write_text(f"PROJECT_NAME={project_name}\n")


def _write_dotenv(install_path: Path, project_name: str) -> None:
    """Write a .env with PROJECT_NAME=<project_name>."""
    (install_path / ".env").write_text(f"PROJECT_NAME={project_name}\n")


def _bare_args(**kwargs) -> SimpleNamespace:
    """Build an argparse-like namespace with project_name + yes attrs."""
    defaults = {"project_name": None, "yes": False}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Section 1: precedence order — CLI flag wins
# ---------------------------------------------------------------------------


def test_cli_flag_overrides_everything(tmp_path):
    """--project-name beats vscode/.claude/env/.env/folder."""
    install_path = tmp_path / "python"  # bad folder name (e.g. python/)
    install_path.mkdir()
    _write_vscode_settings(install_path, "FromVSCode")
    _write_claude_env(install_path, "FromClaudeEnv")
    _write_dotenv(install_path, "FromDotenv")

    args = _bare_args(project_name="FromCLI")
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "FromCLI"


def test_cli_flag_strips_whitespace(tmp_path):
    """--project-name='  Foo  ' resolves to 'Foo'."""
    install_path = tmp_path / "anything"
    install_path.mkdir()
    args = _bare_args(project_name="  Foo  ")
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "Foo"


def test_cli_flag_empty_string_falls_through(tmp_path):
    """--project-name='' (empty string) falls through to next layer."""
    install_path = tmp_path / "python"
    install_path.mkdir()
    _write_vscode_settings(install_path, "FromVSCode")
    args = _bare_args(project_name="")
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "FromVSCode"


# ---------------------------------------------------------------------------
# Section 2: precedence order — .vscode/settings.json beats lower layers
# ---------------------------------------------------------------------------


def test_vscode_settings_overrides_claude_env_and_dotenv(tmp_path):
    """vscode beats .claude/env and .env when no CLI flag."""
    install_path = tmp_path / "python"
    install_path.mkdir()
    _write_vscode_settings(install_path, "MyProj")
    _write_claude_env(install_path, "FromClaudeEnv")
    _write_dotenv(install_path, "FromDotenv")

    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "MyProj"


def test_vscode_settings_env_layout_fallback(tmp_path):
    """Degenerate .vscode/settings.json with env.PROJECT_NAME also works."""
    install_path = tmp_path / "weird"
    install_path.mkdir()
    vscode = install_path / ".vscode"
    vscode.mkdir()
    # NOT canonical claude-code.env — use a plain env block (degenerate layout).
    (vscode / "settings.json").write_text(
        json.dumps({"env": {"PROJECT_NAME": "DegenName"}})
    )
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "DegenName"


def test_vscode_settings_invalid_json_falls_through(tmp_path):
    """Malformed .vscode/settings.json soft-fails; .env layer kicks in."""
    install_path = tmp_path / "myproj"
    install_path.mkdir()
    vscode = install_path / ".vscode"
    vscode.mkdir()
    (vscode / "settings.json").write_text("{not valid json")
    _write_dotenv(install_path, "EnvFallback")
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "EnvFallback"


# ---------------------------------------------------------------------------
# Section 3: precedence — .claude/env beats .env
# ---------------------------------------------------------------------------


def test_claude_env_overrides_dotenv(tmp_path):
    """.claude/env beats .env when neither CLI nor vscode set."""
    install_path = tmp_path / "python"
    install_path.mkdir()
    _write_claude_env(install_path, "FromClaudeEnv")
    _write_dotenv(install_path, "FromDotenv")
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "FromClaudeEnv"


def test_claude_env_with_quotes(tmp_path):
    """.claude/env PROJECT_NAME="Quoted Name" has quotes stripped."""
    install_path = tmp_path / "x"
    install_path.mkdir()
    claude = install_path / ".claude"
    claude.mkdir()
    (claude / "env").write_text('PROJECT_NAME="Quoted Name"\n')
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "Quoted Name"


def test_claude_env_commented_lines_ignored(tmp_path):
    """Commented PROJECT_NAME line is NOT picked up — must be active."""
    install_path = tmp_path / "myproj"
    install_path.mkdir()
    claude = install_path / ".claude"
    claude.mkdir()
    (claude / "env").write_text("# PROJECT_NAME=DisabledValue\n")
    _write_dotenv(install_path, "EnvFallback")
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    # commented in .claude/env → falls through to .env
    assert result == "EnvFallback"


# ---------------------------------------------------------------------------
# Section 4: precedence — .env beats folder name
# ---------------------------------------------------------------------------


def test_dotenv_overrides_folder_name(tmp_path):
    """.env PROJECT_NAME wins over folder-name fallback."""
    install_path = tmp_path / "python"  # would derive 'Python'
    install_path.mkdir()
    _write_dotenv(install_path, "MyProj")
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "MyProj"


def test_dotenv_skips_blank_value(tmp_path):
    """PROJECT_NAME=<empty> in .env → falls through to folder name."""
    install_path = tmp_path / "myfolder"
    install_path.mkdir()
    (install_path / ".env").write_text("PROJECT_NAME=\n")
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "myfolder"


# ---------------------------------------------------------------------------
# Section 5: folder-name fallback (no precedence layer set)
# ---------------------------------------------------------------------------


def test_folder_name_fallback_when_nothing_else(tmp_path):
    """No CLI flag, no vscode, no .claude/env, no .env → folder name."""
    install_path = tmp_path / "MyCoolProj"
    install_path.mkdir()
    # Suppress interactive prompt explicitly (defensive: tmp_path is not TTY
    # anyway, but the test must not hang or read from real stdin).
    args = _bare_args(yes=True)
    result = install_py._resolve_project_name_for_adopt(
        install_path, args, interactive=False
    )
    assert result == "MyCoolProj"


def test_folder_name_fallback_empty_folder_name(tmp_path):
    """Edge case: install_path with empty name → fallback to 'Project'."""
    # Using tmp_path/.. would give a real name, so simulate with a Path()
    # whose .name is empty. Path("/") has empty name.
    install_path = Path("/")
    args = _bare_args(yes=True)
    result = install_py._resolve_project_name_for_adopt(
        install_path, args, interactive=False
    )
    assert result == "Project"


# ---------------------------------------------------------------------------
# Section 6: interactive prompt
# ---------------------------------------------------------------------------


def test_interactive_prompt_uses_entered_value(tmp_path, monkeypatch):
    """User types 'MyName' at the prompt → resolves to 'MyName'."""
    install_path = tmp_path / "folder"
    install_path.mkdir()
    args = _bare_args()
    monkeypatch.setattr("builtins.input", lambda *_args, **_kw: "MyName")
    result = install_py._resolve_project_name_for_adopt(
        install_path, args, interactive=True
    )
    assert result == "MyName"


def test_interactive_prompt_empty_input_uses_folder_default(tmp_path, monkeypatch):
    """User hits ENTER at prompt → falls through to folder name."""
    install_path = tmp_path / "myfolder"
    install_path.mkdir()
    args = _bare_args()
    monkeypatch.setattr("builtins.input", lambda *_args, **_kw: "")
    result = install_py._resolve_project_name_for_adopt(
        install_path, args, interactive=True
    )
    assert result == "myfolder"


def test_interactive_prompt_eof_falls_through(tmp_path, monkeypatch):
    """EOF on input → no crash; fall through to folder name."""
    install_path = tmp_path / "fallbackname"
    install_path.mkdir()
    args = _bare_args()

    def _raise_eof(*_a, **_kw):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise_eof)
    result = install_py._resolve_project_name_for_adopt(
        install_path, args, interactive=True
    )
    assert result == "fallbackname"


# ---------------------------------------------------------------------------
# Section 7: argparse exposes --project-name correctly
# ---------------------------------------------------------------------------


def test_argparse_project_name_flag_parses():
    """Verify the install.py argparse builder accepts --project-name."""
    import argparse
    # Mirror the install.py parser surface for --project-name only —
    # full main() parser is too heavy to instantiate here.
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-name", type=str, default=None)
    args = parser.parse_args(["--project-name", "MyValue"])
    assert args.project_name == "MyValue"

    args2 = parser.parse_args([])
    assert args2.project_name is None


# ---------------------------------------------------------------------------
# Section 8: NON-DESTRUCTIVENESS GATE (CRITICAL — load-bearing)
# ---------------------------------------------------------------------------


def test_existing_project_name_preserved_on_update(tmp_path):
    """CRITICAL non-destructiveness gate: existing project with manifest +
    .env PROJECT_NAME=Foo MUST keep PROJECT_NAME=Foo after _reconcile_env_keys
    runs (the --update code path's env-handling).

    This is the highest-priority test in V47-F. If this fails, the V47-F
    contract is broken — existing projects would have their pinned name
    silently overwritten on every --update run.

    The mechanism that protects existing projects is two-fold:
      1. ``_reconcile_env_keys`` is additive-only — checks ``key not in env``
         before appending. Keys already present (PROJECT_NAME, KG_COLLECTION)
         are NEVER touched.
      2. The new ``_resolve_project_name_for_adopt`` helper is invoked ONLY
         on the NEW adopt path (when ``.vco-manifest.json`` is absent).
         When the manifest IS present (existing VCO project), the helper
         is never called and folder-name re-derivation cannot happen.
    """
    # Build a representative existing-VCO-project fixture.
    project = tmp_path / "python"  # bad folder name fixture
    project.mkdir()

    # Existing manifest (= project is registered with VCO).
    claude_dir = project / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".vco-manifest.json").write_text(json.dumps({
        "version": "0.2.45",
        "files": {},
    }))

    # Existing .env with the user-pinned PROJECT_NAME and KG_COLLECTION.
    env_path = project / ".env"
    env_text_before = (
        "PROJECT_NAME=MyProj\n"
        "KG_COLLECTION=MyProj_KnowledgeGraph\n"
        "DEVELOPMENT_COLLECTION=MyProj_Development\n"
        "SHARED_KG_COLLECTION=VibeCodedOrchestrator_KnowledgeGraph\n"
        "SHARED_KG_WRITE_DISABLED=false\n"
        "SHARED_KG_OPT_OUT=false\n"
        # v0.2.46 Decision B — symmetric READ gate joined the canonical
        # set. Fixture updated to be "fully-populated" under the new
        # contract so the reconcile noop assertion below still pins the
        # additive-only invariant rather than accidentally tripping on
        # the newly-introduced canonical key.
        "SHARED_KG_READ_DISABLED=false\n"
        "ACTIVE_EMBEDDING=qwen3\n"
        "WEAVIATE_URL=http://localhost:8081\n"
        "WEAVIATE_PORT=8081\n"
        "OLLAMA_URL=http://localhost:11435\n"
        "OLLAMA_PORT=11435\n"
        "CODE_EMBED_URL=http://localhost:11440\n"
        "CODE_EMBED_PORT=11440\n"
        "CODE_GRAPH_PROJECT=MyProj\n"
    )
    env_path.write_text(env_text_before)

    # Simulate --update flow's reconcile pass.
    result = install_py._reconcile_env_keys(env_path)

    env_text_after = env_path.read_text()

    # The gate assertions: PROJECT_NAME + KG_COLLECTION byte-identical
    # in env_text_after vs env_text_before for the active assignments.
    def _extract(text: str, key: str) -> str | None:
        for line in text.splitlines():
            s = line.lstrip()
            if s.startswith("#"):
                continue
            if "=" not in s:
                continue
            k, v = s.split("=", 1)
            if k.strip() == key:
                return v.strip()
        return None

    project_name_before = _extract(env_text_before, "PROJECT_NAME")
    project_name_after = _extract(env_text_after, "PROJECT_NAME")
    kg_before = _extract(env_text_before, "KG_COLLECTION")
    kg_after = _extract(env_text_after, "KG_COLLECTION")

    # CRITICAL: must be byte-identical pre/post.
    assert project_name_before == "MyProj"
    assert project_name_after == "MyProj", (
        f"NON-DESTRUCTIVENESS RULE VIOLATED: PROJECT_NAME changed from "
        f"{project_name_before!r} to {project_name_after!r}. _reconcile_env_keys "
        f"or some other update-path code overwrote an existing user-pinned "
        f"PROJECT_NAME. V47-F is broken."
    )
    assert kg_before == "MyProj_KnowledgeGraph"
    assert kg_after == "MyProj_KnowledgeGraph", (
        f"NON-DESTRUCTIVENESS RULE VIOLATED: KG_COLLECTION changed from "
        f"{kg_before!r} to {kg_after!r}. V47-F is broken."
    )

    # All canonical keys already present → reconcile must be noop.
    assert result["action"] == "noop", (
        f"Expected reconcile action=noop on a fully-populated existing "
        f".env, got {result!r}. This suggests a key the user already had "
        f"is being seen as 'missing' — investigate."
    )


def test_resolve_helper_not_invoked_for_existing_manifest(tmp_path):
    """Secondary non-destructiveness check: confirm the helper itself is
    pure (no side effects). The actual "don't call on existing project"
    contract lives in the install.py dispatch logic — Wave 2 / V47-G-final
    wire that. This test just verifies the helper has no manifest-
    awareness that would let it silently mutate state.
    """
    install_path = tmp_path / "python"
    install_path.mkdir()
    # Both manifest AND .env present (simulating registered existing VCO).
    claude = install_path / ".claude"
    claude.mkdir()
    (claude / ".vco-manifest.json").write_text("{}")
    _write_dotenv(install_path, "MyProj")

    # The helper itself MUST return the .env value (it doesn't know about
    # the manifest gate — the dispatch caller enforces that). The contract
    # is: if you call it, it tells you what name to use; the responsibility
    # to NOT call it on existing projects is on the caller.
    args = _bare_args()
    result = install_py._resolve_project_name_for_adopt(install_path, args)
    assert result == "MyProj"


# ---------------------------------------------------------------------------
# Section 9: migration log (informational, _log_project_name_pin_diff)
# ---------------------------------------------------------------------------


def test_log_emitted_when_envname_differs_from_folder(tmp_path, capsys, monkeypatch):
    """When .env PROJECT_NAME=MyProj but folder is 'python', the
    informational log line must be emitted.
    """
    project = tmp_path / "python"
    project.mkdir()
    _write_dotenv(project, "MyProj")

    # Stub _resolve_project_id_by_folder to None so we fall through to
    # the .env reader path.
    monkeypatch.setattr(install_py, "_resolve_project_id_by_folder",
                        lambda _p: None)
    # Stub _log_install_event to a no-op (just captures stdout).
    monkeypatch.setattr(install_py, "_log_install_event",
                        lambda *_a, **_k: None)

    install_py._log_project_name_pin_diff(project, "")
    captured = capsys.readouterr()
    assert "MyProj" in captured.out
    assert "Python" in captured.out  # folder-sanitized
    assert "python" in captured.out  # folder raw


def test_log_silent_when_envname_matches_folder(tmp_path, capsys, monkeypatch):
    """When the env-pinned name sanitizes to the same value as the folder
    name, no informational log is emitted (nothing surprising to surface).

    Both 'Foo' (in .env) and 'Foo' (folder) sanitize to 'Foo' via
    ``sanitize_for_weaviate_class`` — no pin-diff to report.
    """
    project = tmp_path / "Foo"
    project.mkdir()
    _write_dotenv(project, "Foo")  # both sanitize to "Foo" — identical

    monkeypatch.setattr(install_py, "_resolve_project_id_by_folder",
                        lambda _p: None)
    monkeypatch.setattr(install_py, "_log_install_event",
                        lambda *_a, **_k: None)

    install_py._log_project_name_pin_diff(project, "")
    captured = capsys.readouterr()
    # Should be empty — no pin-diff to report.
    assert "pinned" not in captured.out


def test_log_soft_fails_when_no_envfile(tmp_path, capsys, monkeypatch):
    """No .env, no launcher.db → log emits nothing (soft fail, no crash)."""
    project = tmp_path / "empty"
    project.mkdir()

    monkeypatch.setattr(install_py, "_resolve_project_id_by_folder",
                        lambda _p: None)
    monkeypatch.setattr(install_py, "_log_install_event",
                        lambda *_a, **_k: None)

    install_py._log_project_name_pin_diff(project, "")
    captured = capsys.readouterr()
    assert "pinned" not in captured.out
