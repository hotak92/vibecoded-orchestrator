# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""D-11 lean-ctx candidate probe + SEC-RAW credential step-aside.

v0.2.101 ALLOW-LIST INVERSION changed what this file pins:

  * D-11 (KEPT): ``templates/hooks/lean-ctx-rewrite.sh`` (+ ``.ps1``) probe
    the same candidate list ``install.py::_find_lean_ctx_binary`` uses
    (``~/.cargo/bin`` → ``~/.local/bin`` → ``/usr/local/bin`` → ``/usr/bin``
    → homebrew) before giving up. A ``cargo install``ed binary off the hook
    shell's PATH still activates compression. The extraction-based parity
    lock over all three lists lives in
    ``tests/test_v0295_wp7_bootstrap_cascade_parity.py``.
  * SEC-RAW (KEPT through the inversion): allow-listed commands CAN carry
    credentials (``pip install --index-url https://user:pass@host/simple``,
    ``curl -u``/auth headers, ``wget --password``, npm ``_authToken``
    args, secret-shaped env prefixes), so the credential scan still runs
    BEFORE the allow-list and any hit forces the raw path. The pattern
    list stays a C-mirror between the siblings, parity-pinned below by
    extraction (not a source scan).
  * TRIM-b / TRIM-r (RETIRED): the ``git commit``/``git push`` step-aside
    and the seven read-only git verbs are subsumed by the allow-list — no
    git form is allow-listed, so ALL git runs raw and the verb tables are
    gone from both hooks. The behavioural allow-list cases (wrap/raw
    tables, wrapper tee/pointer, TTL sweep, .sh/.ps1 parity) live in
    ``tests/test_v02101_lean_ctx_allowlist_tee.py``; the git-is-never-
    wrapped representative arms remain here.

The .ps1 behavioural cases are pwsh-gated; structural parity is covered
by the hook-OS-parity gate.
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

# The pre-v0.2.101 hook delegated the wrap decision to the lean-ctx binary
# and filtered ITS response. Since the allow-list inversion the hook
# constructs the response itself and never invokes the binary at rewrite
# time — the fake's canned stdout is deliberately IGNORED by the hook; it
# only needs to EXIST for the candidate probe. (The retirement is pinned
# behaviourally by test_hook_never_invokes_the_binary_* in
# tests/test_v02101_lean_ctx_allowlist_tee.py.)
ALLOW_RESPONSE = (
    '{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
    '"permissionDecision":"allow",'
    '"updatedInput":{"command":"lean-ctx -c \'ls\'"}}}'
)

#: An allow-listed command (see templates/hooks/_lib/lean-ctx-allowlist.txt).
WRAPPED_CMD = "npm install"


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
    # tests/conftest.py pins CLAUDE_PROJECT_DIR to a suite-wide scratch
    # project; these tests stage their own project cwd.
    env.pop("CLAUDE_PROJECT_DIR", None)
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
        res = _run_sh(WRAPPED_CMD, cargo_bin=cargo_bin, strip_path=True,
                      fake_home=home)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, "candidate-probe should have found ~/.cargo/bin binary"
        data = json.loads(out)
        wrapped = data["hookSpecificOutput"]["updatedInput"]["command"]
        assert "lean-ctx-tee.sh" in wrapped, (
            f"rewrite must route through the tee wrapper: {wrapped}"
        )
        assert str(cargo_bin / "lean-ctx") in wrapped, (
            f"the probed binary path must be threaded into the wrapper "
            f"call: {wrapped}"
        )
        # D-3 invariant, now structural: the hook builds the response
        # itself, so no auto-approval field can appear.
        assert "permissionDecision" not in data["hookSpecificOutput"]

    def test_binary_absent_everywhere_clean_noop(self, tmp_path):
        """No binary on PATH or any candidate → clean exit-0 no-op
        (leave-alone)."""
        home = tmp_path / "home"
        (home / "proj").mkdir(parents=True)
        res = _run_sh(WRAPPED_CMD, cargo_bin=None, strip_path=True,
                      fake_home=home)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", "absent binary must be a no-op"


class TestGitNeverWrapped:
    """RETIRED MACHINERY, SAME OUTCOME: TRIM-b (git commit/push) and
    TRIM-r (the seven read-only inspection verbs) were command-specific
    step-asides inside a compress-everything rule. Under the v0.2.101
    allow-list NO git form is a candidate at all — every git command runs
    raw by omission, and the verb tables no longer exist in the hooks."""

    def _run_with_binary(self, cmd: str, tmp_path: Path):
        home = tmp_path / "home"
        _make_fake_lean_ctx(home / ".cargo" / "bin")
        return _run_sh(cmd, cargo_bin=home / ".cargo" / "bin",
                       strip_path=True, fake_home=home)

    @pytest.mark.parametrize("cmd", [
        'git commit -m "x"',
        "git push origin main",
        "git show HEAD",
        "git diff HEAD~1",
        "git log --oneline -500",
        "git ls-tree -r HEAD --name-only",
        "git status",
        "git branch",
        "git fetch origin",
        "git log && git commit -m y",
        "echo git commit",
    ])
    def test_git_form_runs_raw(self, cmd, tmp_path):
        res = self._run_with_binary(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            f"no git form may be wrapped (allow-list omits git): {cmd}"
        )

    def test_allow_listed_command_still_wrapped(self, tmp_path):
        res = self._run_with_binary(WRAPPED_CMD, tmp_path)
        assert res.stdout.strip() != "", "allow-listed command must wrap"

    def test_verb_table_retired_from_both_hooks(self):
        for hook in (SH_HOOK, PS1_HOOK):
            src = hook.read_text(encoding="utf-8-sig")
            assert "GIT-READONLY-VERBS" not in src, (
                f"{hook.name}: the TRIM-r verb table must stay retired"
            )


def test_sh_source_mentions_must_match_ps1():
    """Marker pin only — NOT the candidate-order parity guarantee.

    This asserts that the D-11 / allow-list *markers* survive an edit of
    either hook. It is a source scan, so a name inside a comment satisfies
    it. The real, extraction-based parity assertion over install.py /
    lean-ctx-rewrite.sh / lean-ctx-rewrite.ps1 lives in
    ``tests/test_v0295_wp7_bootstrap_cascade_parity.py``
    (``test_lean_ctx_cascade_agrees_across_install_py_and_both_hooks``).
    Keep both; they check different things.
    """
    sh = SH_HOOK.read_text(encoding="utf-8")
    ps1 = PS1_HOOK.read_text(encoding="utf-8-sig")
    for src in (sh, ps1):
        assert ".cargo/bin/lean-ctx" in src, "candidate probe missing"
        assert "lean-ctx-allowlist.txt" in src, "shared allow-list missing"
        assert "MUST MATCH" in src


# ─── SEC-RAW: credential-bearing commands step aside (2026-07-21) ────────
# KEPT through the v0.2.101 allow-list inversion because allow-listed
# commands CAN carry credentials: pip install --index-url
# https://user:pass@host/simple, curl -u / auth headers, wget --password,
# npm registry _authToken args, secret-shaped env prefixes. The hook scans
# the WHOLE command against the pattern list and emits nothing (raw) on
# any hit — before the allow-list is ever consulted.


class TestSecRawSecretsStepAside:
    def _run_with_binary(self, cmd: str, tmp_path: Path):
        home = tmp_path / "home"
        _make_fake_lean_ctx(home / ".cargo" / "bin")
        return _run_sh(cmd, cargo_bin=home / ".cargo" / "bin",
                       strip_path=True, fake_home=home)

    @pytest.mark.parametrize("cmd", [
        'curl -s -H "Authorization: Bearer ATATTfaketok12345" https://x.test/',
        "curl -s -u user@example.test:ATATTfaketok12345 https://x.test/",
        'curl -H "X-Api-Key: abcdef123456" https://x.test/',
        'T=$JIRA_TOKEN; curl -s https://x.test/',
        "MY_API_KEY=abc123 ./run.sh",
        "vct exec --secret k=ENV -- cmd",
        ".claude/scripts/vct_secrets_resolve.sh . github_pat",
        "echo ghp_0123456789abcdef",
        'wget --password hunter2 https://x.test/',
        # credential in a NON-final && segment still disqualifies the wrap
        'curl -u a@b.test:tok123 https://x.test/ && echo done',
        # v0.2.101 additions: allow-listed commands carrying credentials
        "pip install --index-url https://user:secret123@pypi.test/simple pkg",
        "npm install --//registry.npmjs.org/:_authToken=abc12345",
        "MY_TOKEN=x npm install",
    ])
    def test_credential_command_passes_through_raw(self, cmd, tmp_path):
        res = self._run_with_binary(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            f"credential-bearing command must run raw, got rewrite for: {cmd}"
        )

    @pytest.mark.parametrize("cmd", [
        # benign AND allow-listed — a benign non-allow-listed command runs
        # raw too, but by the allow-list gate, not SEC-RAW (that table is
        # in tests/test_v02101_lean_ctx_allowlist_tee.py).
        "npm install",
        "pip install requests",
        "curl -s https://example.test/health",
        "python3 -m pytest tests/ -q",
    ])
    def test_benign_allow_listed_command_still_wrapped(self, cmd, tmp_path):
        res = self._run_with_binary(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() != "", (
            f"benign allow-listed command must still be wrapped: {cmd}"
        )


def _extract_patterns(src: str, quote: str) -> list[str]:
    """Pull the pattern literals between the SEC-RAW markers. `quote` is the
    string delimiter used by that language ('"' for the sh-embedded python
    raw strings, "'" for PowerShell)."""
    begin = src.index("SEC-RAW-PATTERNS-BEGIN")
    end = src.index("SEC-RAW-PATTERNS-END")
    block = src[begin:end]
    out = []
    for line in block.splitlines():
        line = line.strip().rstrip(",")
        if quote == '"' and line.startswith('r"') and line.endswith('"'):
            out.append(line[2:-1])
        elif quote == "'" and line.startswith("'") and line.endswith("'"):
            out.append(line[1:-1])
    return out


def test_sec_raw_pattern_list_parity_sh_ps1():
    """The credential pattern lists in the two siblings are byte-identical
    (C-mirror discipline: same data, thin per-language wrapper)."""
    sh_patterns = _extract_patterns(SH_HOOK.read_text(encoding="utf-8"), '"')
    ps1_patterns = _extract_patterns(PS1_HOOK.read_text(encoding="utf-8-sig"), "'")
    assert sh_patterns, "sh SEC-RAW pattern block missing or unparsed"
    assert sh_patterns == ps1_patterns, (
        "SEC-RAW pattern lists diverged between .sh and .ps1"
    )


def test_sec_raw_patterns_are_valid_in_python_re():
    """Every shipped pattern must compile — a bad pattern would make the
    scan raise and silently fall back to the compressed path."""
    import re as _re
    for p in _extract_patterns(SH_HOOK.read_text(encoding="utf-8"), '"'):
        _re.compile(p)


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
        env.pop("CLAUDE_PROJECT_DIR", None)
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

    def test_ps1_git_inspection_verb_passthrough(self, tmp_path):
        res = self._run_ps1("git show HEAD", tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            "ps1 git inspection form must pass through raw"
        )

    def test_ps1_allow_listed_command_rewritten(self, tmp_path):
        res = self._run_ps1(WRAPPED_CMD, tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() != "", "ps1 allow-listed command must wrap"
        data = json.loads(res.stdout.strip())
        wrapped = data["hookSpecificOutput"]["updatedInput"]["command"]
        assert "lean-ctx-tee.ps1" in wrapped, wrapped

    def test_ps1_credential_command_passes_through_raw(self, tmp_path):
        res = self._run_ps1(
            'curl -s -H "Authorization: Bearer ATATTfaketok12345" https://x.test/',
            tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            "ps1 credential-bearing command must run raw"
        )

    def test_ps1_unknown_command_passthrough(self, tmp_path):
        res = self._run_ps1("ls -la", tmp_path, on_path=True)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            "ps1 non-allow-listed command must run raw"
        )


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
