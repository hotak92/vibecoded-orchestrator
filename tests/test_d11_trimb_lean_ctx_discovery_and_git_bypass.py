# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-11 + TRIM-b (v0.2.75): lean-ctx hook candidate probe + git step-aside.

Two fixes land in ONE commit (landing D-11 alone would re-arm the
git-commit stderr-swallow footgun):

  * D-11: ``templates/hooks/lean-ctx-rewrite.sh`` (+ ``.ps1``) now probe
    the same candidate list ``install.py::_find_lean_ctx_binary`` uses
    (``~/.cargo/bin`` → ``~/.local/bin`` → ``/usr/local/bin`` →
    ``/usr/bin`` → homebrew) before giving up. A ``cargo install``ed
    binary off the hook shell's PATH now still activates compression.
  * TRIM-b: the hook auto-bypasses ``git commit`` / ``git push`` — it
    emits nothing (raw command) so a hook-failed commit's stderr is never
    swallowed. Keyed on the FINAL ``&&``-segment so ``git log && git
    commit`` passes through while ``echo git commit`` is still rewritten.

Both fixes are mirrored .sh/.ps1 (must-match). The .ps1 behavioural cases
are pwsh-gated; structural parity is covered by the hook-OS-parity gate.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.sh"
PS1_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.ps1"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook; .ps1 behavioural cases are pwsh-gated below.",
)

# lean-ctx's canned rewrite response (matches 3.4.5 serialization).
ALLOW_RESPONSE = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    '"permissionDecision":"allow",'
    '"updatedInput":{"command":"lean-ctx -c \'ls\'"}}}'
)


def _payload(cmd: str) -> str:
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": {"command": cmd}}
    )


def _make_fake_lean_ctx(bin_dir: Path) -> Path:
    """A fake lean-ctx that consumes stdin and prints ALLOW_RESPONSE.
    Returns the binary path (extensionless, POSIX)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    resp = bin_dir / "response.json"
    resp.write_text(ALLOW_RESPONSE, encoding="utf-8")
    fake = bin_dir / "lean-ctx"
    fake.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            cat > /dev/null
            cat '{resp}'
        """),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_sh(cmd: str, *, cargo_bin: Path | None, strip_path: bool,
            fake_home: Path) -> subprocess.CompletedProcess:
    """Run the .sh hook. When cargo_bin is set, a fake lean-ctx is placed
    at $fake_home/.cargo/bin (NOT on PATH when strip_path=True), so only the
    D-11 candidate probe (keyed on $HOME) can find it."""
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    env["HOME"] = str(fake_home)
    if strip_path:
        # A minimal PATH that still has bash/python but NOT the fake bindir.
        env["PATH"] = "/usr/bin:/bin"
    cwd = fake_home / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(SH_HOOK)],
        input=_payload(cmd), capture_output=True, text=True,
        cwd=cwd, env=env, timeout=30,
    )


class TestD11CandidateProbe:
    def test_binary_only_at_cargo_bin_off_path_still_rewrites(self, tmp_path):
        """Binary staged ONLY at ~/.cargo/bin with a stripped PATH → the
        D-11 candidate probe finds it and the rewrite fires (act)."""
        home = tmp_path / "home"
        cargo_bin = home / ".cargo" / "bin"
        _make_fake_lean_ctx(cargo_bin)
        res = _run_sh("ls -la", cargo_bin=cargo_bin, strip_path=True,
                      fake_home=home)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, "candidate-probe should have found ~/.cargo/bin binary"
        data = json.loads(out)
        assert data["hookSpecificOutput"]["updatedInput"]["command"] == (
            "lean-ctx -c 'ls'"
        )
        # D-3 invariant still holds: permissionDecision stripped.
        assert "permissionDecision" not in data["hookSpecificOutput"]

    def test_binary_absent_everywhere_clean_noop(self, tmp_path):
        """No binary on PATH or any candidate → clean exit-0 no-op
        (leave-alone)."""
        home = tmp_path / "home"
        (home / "proj").mkdir(parents=True)
        res = _run_sh("ls -la", cargo_bin=None, strip_path=True,
                      fake_home=home)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "absent binary must be a no-op"


class TestTrimBGitBypass:
    def _run_with_binary(self, cmd: str, tmp_path: Path):
        home = tmp_path / "home"
        _make_fake_lean_ctx(home / ".cargo" / "bin")
        return _run_sh(cmd, cargo_bin=home / ".cargo" / "bin",
                       strip_path=True, fake_home=home)

    def test_git_commit_passthrough(self, tmp_path):
        res = self._run_with_binary('git commit -m "x"', tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "git commit must pass through raw"

    def test_git_push_passthrough(self, tmp_path):
        res = self._run_with_binary("git push origin main", tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "git push must pass through raw"

    def test_final_segment_governs_git_commit(self, tmp_path):
        # `git log && git commit` → the FINAL segment (commit) governs → raw.
        res = self._run_with_binary("git log && git commit -m y", tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "final && segment (commit) must govern"

    def test_echo_git_commit_still_rewritten(self, tmp_path):
        # `echo git commit` is NOT a real commit → still compressed (act).
        res = self._run_with_binary("echo git commit", tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() != "", "echo git commit must still be rewritten"

    def test_commit_as_first_segment_not_bypassed(self, tmp_path):
        # `git commit && echo done` → final segment is `echo done`, NOT a
        # commit → compressed. (The commit's own stderr is a separate call.)
        res = self._run_with_binary("git commit -m z && echo done", tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() != "", (
            "when commit is not the final segment, normal path applies"
        )

    def test_ordinary_command_still_rewritten(self, tmp_path):
        res = self._run_with_binary("ls -la", tmp_path)
        assert res.stdout.strip() != "", "non-git command must still rewrite"


def test_sh_source_mentions_must_match_ps1():
    """Regression pin: both siblings carry the D-11 candidate order + the
    TRIM-b git step-aside (must-match discipline)."""
    sh = SH_HOOK.read_text(encoding="utf-8")
    ps1 = PS1_HOOK.read_text(encoding="utf-8")
    for src in (sh, ps1):
        assert ".cargo/bin/lean-ctx" in src, "candidate probe missing"
        assert "commit" in src and "push" in src, "git step-aside missing"
        assert "MUST MATCH" in src


# ─── pwsh-gated .ps1 behavioural parity ──────────────────────────────────


def _make_fake_lean_ctx_ps(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    resp = bin_dir / "response.json"
    resp.write_text(ALLOW_RESPONSE, encoding="utf-8")
    (bin_dir / "lean-ctx.ps1").write_text(
        "$null = $input\n"
        f"Write-Output (Get-Content -Raw -LiteralPath '{resp}')\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
class TestPs1Parity:
    def _run_ps1(self, cmd: str, tmp_path: Path, *, on_path: bool):
        bin_dir = tmp_path / "fakebin"
        _make_fake_lean_ctx_ps(bin_dir)
        env = dict(os.environ)
        env.pop("VCT_DISABLE_HOOKS", None)
        if on_path:
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        cwd = tmp_path / "proj"
        cwd.mkdir(exist_ok=True)
        return subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(PS1_HOOK)],
            input=_payload(cmd), capture_output=True, text=True,
            cwd=cwd, env=env, timeout=30,
        )

    def test_ps1_git_commit_passthrough(self, tmp_path):
        res = self._run_ps1('git commit -m "x"', tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "ps1 git commit must pass through"

    def test_ps1_ordinary_command_rewritten(self, tmp_path):
        res = self._run_ps1("ls -la", tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() != "", "ps1 non-git command must rewrite"


# ─── vco_lib.install_companions.ensure_discovered_lean_ctx_on_path ───────
# D-11 installer half was extracted to vco_lib (install.py soft line-ratchet).


def _import_companions():
    sys.path.insert(0, str(REPO_ROOT))
    import importlib
    from vco_lib import install_companions  # type: ignore
    return importlib.reload(install_companions)


class TestInstallDiscoveredCopy:
    """D-11 installer half: copy a DISCOVERED off-PATH binary into
    ~/.local/bin so a minimal hook shell resolves it. home/os_name are
    injected (no monkeypatching of Path.home / platform needed)."""

    def test_copies_off_path_binary_to_local_bin(self, tmp_path, monkeypatch):
        mod = _import_companions()
        home = tmp_path / "home"
        cargo = home / ".cargo" / "bin"
        cargo.mkdir(parents=True)
        src = cargo / "lean-ctx"
        src.write_text("#!/bin/sh\necho fake\n")
        src.chmod(0o755)
        # NOT on PATH → which returns None → copy should happen.
        monkeypatch.setattr(mod.shutil, "which", lambda _n: None)

        dest = mod.ensure_discovered_lean_ctx_on_path(
            str(src), home=home, os_name="Linux")
        assert dest is not None
        assert Path(dest) == home / ".local" / "bin" / "lean-ctx"
        assert Path(dest).is_file()
        assert os.access(dest, os.X_OK)

    def test_skips_when_already_on_path(self, tmp_path, monkeypatch):
        mod = _import_companions()
        home = tmp_path / "home"
        cargo = home / ".cargo" / "bin"
        cargo.mkdir(parents=True)
        src = cargo / "lean-ctx"
        src.write_text("x")
        # Already on PATH → no copy (leave-alone).
        monkeypatch.setattr(mod.shutil, "which", lambda _n: str(src))

        dest = mod.ensure_discovered_lean_ctx_on_path(
            str(src), home=home, os_name="Linux")
        assert dest is None
        assert not (home / ".local" / "bin" / "lean-ctx").exists()

    def test_missing_source_is_noop(self, tmp_path, monkeypatch):
        mod = _import_companions()
        monkeypatch.setattr(mod.shutil, "which", lambda _n: None)
        dest = mod.ensure_discovered_lean_ctx_on_path(
            str(tmp_path / "does-not-exist"), home=tmp_path, os_name="Linux")
        assert dest is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
