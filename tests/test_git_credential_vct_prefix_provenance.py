# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""E-5 — `git-credential-vct` prefix-fallback provenance + owner gate.

The helper resolves a project-scoped `github_pat` two ways:
  1. an explicit `.vct-project` marker walked up from $PWD (safe), or
  2. a prefix strip of $PWD against `$VCT_PROJECT_ROOT_PATTERN` that treats
     the next path segment as the project name — with NO verification that
     the derived name matches the repo's actual remote/owner.

Before E-5, path (2) could silently serve a DIFFERENT project's PAT (or
fall through to the shared PAT) with no indication (finding
`.claude/context/reviews/v0273-fable-review/findings/E-findings.md` §E-5).

This suite pins the fix:
  * prefix-derived resolution emits a one-line stderr provenance note
    naming the KEY + SCOPE (never the VALUE);
  * `.vct-project`-marker resolution stays silent;
  * the served token VALUE never appears on stderr;
  * the opt-in owner gate (`VCT_GIT_CREDENTIAL_OWNER_GATE=1`) refuses a
    prefix-derived project-scoped PAT when the repo's github.com owner
    does not match the derived project name.

All fixtures are synthetic — placeholder token strings, `example`-style
owners; no real credentials, no project-identifying strings.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CRED_HELPER = REPO_ROOT / "tools" / "vct-secrets" / "git-credential-vct"

# Synthetic values — NOT real secrets, and deliberately NOT github-token-
# shaped (no `ghp_`/`github_pat_` prefix) so leak/privacy gates don't flag
# the fixture. The helper serves whatever string is in the file verbatim;
# the shape is irrelevant to what E-5 exercises (WHICH file was chosen).
FAKE_SHARED_PAT = "synthetic-shared-token-not-a-real-secret"
FAKE_PROJECT_PAT = "synthetic-project-token-not-a-real-secret"
GET_STDIN = "protocol=https\nhost=github.com\n\n"


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _run_helper(
    *,
    cwd: Path,
    secrets_dir: Path,
    root_pattern: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd.parent),
        "VCT_SECRETS_DIR": str(secrets_dir),
        "VCT_PROJECT_ROOT_PATTERN": str(root_pattern),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(CRED_HELPER), "get"],
        input=GET_STDIN,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=30,
    )


def test_prefix_fallback_emits_provenance_note(tmp_path):
    root = tmp_path / "dev"
    workdir = root / "projectX" / "sub"
    workdir.mkdir(parents=True)
    secrets = tmp_path / "store"
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)
    _write_secret(secrets / "projects" / "projectX" / "github_pat", FAKE_PROJECT_PAT)

    r = _run_helper(cwd=workdir, secrets_dir=secrets, root_pattern=root)

    assert r.returncode == 0, r.stderr
    # The project-scoped PAT was served on stdout...
    assert f"password={FAKE_PROJECT_PAT}" in r.stdout, r.stdout
    # ...and a provenance note fired on stderr naming key + scope.
    assert "git-credential-vct" in r.stderr, r.stderr
    assert "github_pat" in r.stderr
    assert "prefix-derived" in r.stderr
    assert "projectX" in r.stderr
    # VALUE must NEVER appear in stderr.
    assert FAKE_PROJECT_PAT not in r.stderr, "token value leaked to stderr!"
    assert FAKE_SHARED_PAT not in r.stderr


def test_marker_resolution_is_silent(tmp_path):
    root = tmp_path / "dev"
    workdir = root / "projectX" / "sub"
    workdir.mkdir(parents=True)
    # Explicit marker at the project root → safe path, no provenance note.
    (root / "projectX" / ".vct-project").write_text("projectX\n", encoding="utf-8")
    secrets = tmp_path / "store"
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)
    _write_secret(secrets / "projects" / "projectX" / "github_pat", FAKE_PROJECT_PAT)

    r = _run_helper(cwd=workdir, secrets_dir=secrets, root_pattern=root)

    assert r.returncode == 0, r.stderr
    assert f"password={FAKE_PROJECT_PAT}" in r.stdout, r.stdout
    # No prefix-fallback provenance note when the marker resolved it.
    assert "prefix-derived" not in r.stderr, r.stderr
    assert FAKE_PROJECT_PAT not in r.stderr


def test_prefix_fallthrough_to_shared_notes_shared_scope(tmp_path):
    root = tmp_path / "dev"
    workdir = root / "projectY" / "sub"
    workdir.mkdir(parents=True)
    secrets = tmp_path / "store"
    # Only a shared PAT — no project-scoped file for projectY.
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)

    r = _run_helper(cwd=workdir, secrets_dir=secrets, root_pattern=root)

    assert r.returncode == 0, r.stderr
    assert f"password={FAKE_SHARED_PAT}" in r.stdout, r.stdout
    # Note should say it fell through to shared for the derived project.
    assert "shared" in r.stderr, r.stderr
    assert "projectY" in r.stderr
    assert FAKE_SHARED_PAT not in r.stderr


def test_owner_gate_blocks_mismatched_prefix_project(tmp_path):
    """With the opt-in owner gate on, a prefix-derived project whose PAT
    would be served but whose github.com owner differs is refused."""
    if not _git_available():
        pytest.skip("git not available to set up a remote")
    root = tmp_path / "dev"
    repo = root / "projectX"
    repo.mkdir(parents=True)
    secrets = tmp_path / "store"
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)
    _write_secret(secrets / "projects" / "projectX" / "github_pat", FAKE_PROJECT_PAT)

    # Init a repo whose origin owner ('someoneelse') != derived project 'projectX'.
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin",
         "https://github.com/someoneelse/projectX.git")

    r = _run_helper(
        cwd=repo,
        secrets_dir=secrets,
        root_pattern=root,
        extra_env={"VCT_GIT_CREDENTIAL_OWNER_GATE": "1"},
    )

    assert r.returncode == 0, r.stderr
    # Owner gate refuses the project-scoped PAT → falls back to shared.
    assert f"password={FAKE_SHARED_PAT}" in r.stdout, r.stdout
    assert FAKE_PROJECT_PAT not in r.stdout, "mismatched project PAT was served!"
    assert "does not match" in r.stderr, r.stderr
    assert FAKE_PROJECT_PAT not in r.stderr
    assert FAKE_SHARED_PAT not in r.stderr


def test_owner_gate_allows_matching_prefix_project(tmp_path):
    if not _git_available():
        pytest.skip("git not available to set up a remote")
    root = tmp_path / "dev"
    repo = root / "projectX"
    repo.mkdir(parents=True)
    secrets = tmp_path / "store"
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)
    _write_secret(secrets / "projects" / "projectX" / "github_pat", FAKE_PROJECT_PAT)

    _git(repo, "init", "-q")
    # Owner ('projectX') matches the derived project name (case-insensitive).
    _git(repo, "remote", "add", "origin",
         "https://github.com/projectX/somerepo.git")

    r = _run_helper(
        cwd=repo,
        secrets_dir=secrets,
        root_pattern=root,
        extra_env={"VCT_GIT_CREDENTIAL_OWNER_GATE": "1"},
    )

    assert r.returncode == 0, r.stderr
    # Matching owner → project-scoped PAT served.
    assert f"password={FAKE_PROJECT_PAT}" in r.stdout, r.stdout
    assert "does not match" not in r.stderr, r.stderr


def test_quiet_env_suppresses_provenance(tmp_path):
    root = tmp_path / "dev"
    workdir = root / "projectX" / "sub"
    workdir.mkdir(parents=True)
    secrets = tmp_path / "store"
    _write_secret(secrets / "shared" / "github_pat", FAKE_SHARED_PAT)
    _write_secret(secrets / "projects" / "projectX" / "github_pat", FAKE_PROJECT_PAT)

    r = _run_helper(
        cwd=workdir,
        secrets_dir=secrets,
        root_pattern=root,
        extra_env={"VCT_GIT_CREDENTIAL_QUIET": "1"},
    )

    assert r.returncode == 0, r.stderr
    assert f"password={FAKE_PROJECT_PAT}" in r.stdout, r.stdout
    assert r.stderr.strip() == "", f"expected silent stderr, got: {r.stderr!r}"


# ─── helpers ────────────────────────────────────────────────────────────


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(repo)},
    )
