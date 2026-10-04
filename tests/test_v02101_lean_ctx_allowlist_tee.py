# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101: lean-ctx allow-list inversion + lossless tee/pointer.

Owner ruling 2026-10-03 (PLAN-V0300 item 12): KEEP lean-ctx, but

  1. the rewrite hooks compress ONLY commands on one committed allow-list
     (``templates/hooks/_lib/lean-ctx-allowlist.txt`` — a single rule table
     both siblings PARSE, A>B>C tier B). Loops, pipes, redirects, ``git``,
     unknown commands and anything credential-bearing run RAW. The old
     exemption machinery (TRIM-b git commit/push step-aside, TRIM-r
     read-only git verbs, delegation to the upstream rewrite handler) is
     retired — the allow-list subsumes it.
  2. every compressed run is LOSSLESS: the rewritten command is a wrapper
     (``_lib/lean-ctx-tee.sh`` / ``.ps1``) that tees the full raw output to
     ``<project>/.claude/state/lean-ctx-tee/<ts>-<pid>-<ck>.log`` and ends
     the compressed output with one pointer line. TTL sweep (default 168 h,
     knob ``VCO_LEAN_CTX_TEE_TTL_HOURS`` from ``.claude/env``; 0 = keep
     forever) deletes stale ``.log``/``.cmd`` files on every wrapped run.

SEC-RAW is KEPT because allow-listed commands can carry credentials
(``pip install --index-url https://user:pass@host/simple``, ``curl -u``,
``wget --password``, npm registry ``_authToken`` args, secret-shaped env
prefixes) — extended with a URL-userinfo and an ``_authToken=`` pattern.

Behavioural cases run the SAME table against both siblings (the repo's
hook-parity pattern); .ps1 arms are pwsh-gated.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SH_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.sh"
PS1_HOOK = REPO_ROOT / "templates" / "hooks" / "lean-ctx-rewrite.ps1"
TEE_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "lean-ctx-tee.sh"
TEE_PS1 = REPO_ROOT / "templates" / "hooks" / "_lib" / "lean-ctx-tee.ps1"
ALLOWLIST = REPO_ROOT / "templates" / "hooks" / "_lib" / "lean-ctx-allowlist.txt"

_HAS_PWSH = sys.platform != "win32" and subprocess.run(
    ["which", "pwsh"], capture_output=True).returncode == 0

# ─── case tables (identical for both siblings) ───────────────────────────

WRAP_CASES = [
    "npm install",
    "npm ci",
    "pnpm add foo",
    "yarn install",
    "pip install requests",
    "pip3 install -e .",
    "python3 -m pip install pytest",
    ".venv/bin/python -m pytest tests -q",
    "PYTHONPATH=$PWD pytest tests",
    "CI=1 uv pip install -r reqs.txt",
    "cargo install ripgrep",
    "docker pull alpine",
    "podman pull docker.io/library/alpine",
    "podman-compose pull",
    "docker compose build",
    "wget https://example.test/f.tar.gz",
    "curl -sS https://example.test/health",
    "pytest tests -q",
    "cargo build --workspace",
    "cargo test",
    "cargo clippy",
    "tsc --noEmit",
    "vitest run",
    "npx vitest run",
    "npm run build",
    "npm test",
    "pnpm build",
    "yarn test",
]

RAW_CASES = [
    # unknown commands
    "ls -la",
    "echo hello",
    "cat file.txt",
    "mytool run --flag",
    # git — every verb, by allow-list omission (TRIM-b/TRIM-r retired)
    "git status",
    "git commit -m x",
    "git push origin main",
    "git show HEAD",
    "git ls-tree -r HEAD --name-only",
    "git log && git commit -m y",
    # pipes / chains / loops / redirects
    "npm install | tail -5",
    "npm install && curl -s https://x.test",
    "for f in *.py; do echo $f; done",
    "npm install > log.txt",
    "npm install\necho done",
    "echo $(npm install)",
    # allow-list is prefix-anchored on whole tokens
    "npm",
    "npm uninstall foo",
    "npm installx",
    "cargo run",
    # credentials (SEC-RAW kept — allow-listed commands can carry them)
    "pip install --index-url https://user:secret123@pypi.test/simple pkg",
    'curl -s -H "Authorization: Bearer ATATTfaketok12345" https://x.test/',
    "curl -s -u user@example.test:ATATTfaketok12345 https://x.test/",
    "npm install --//registry.npmjs.org/:_authToken=abc12345",
    "wget --password hunter2 https://x.test/f",
    "MY_API_KEY=abc123 pip install requests",
    "vct exec --secret k=ENV -- npm install",
    ".claude/scripts/vct_secrets_resolve.sh . github_pat",
    # per-call lean-ctx forms step aside (no double-wrap)
    'lean-ctx bypass "npm install"',
    'lean-ctx -c "npm install"',
]


def _payload(cmd: str, **extra_tool_input) -> str:
    tool_input = {"command": cmd}
    tool_input.update(extra_tool_input)
    return json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash",
         "tool_input": tool_input}
    )


def _make_fake_lean_ctx(bin_dir: Path, *, failing: bool = False) -> Path:
    """A fake lean-ctx: logs argv to $FAKE_ARGV_LOG (when set), consumes
    stdin, prints a marker (ok flavour) or exits 1 (failing flavour)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "lean-ctx"
    body = "#!/usr/bin/env bash\n"
    body += 'if [ -n "${FAKE_ARGV_LOG:-}" ]; then printf \'%s\\n\' "$*" >> "$FAKE_ARGV_LOG"; fi\n'
    body += "cat > /dev/null\n"
    body += "exit 1\n" if failing else "printf 'COMPRESSED-BY-FAKE\\n'\n"
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _run_sh_hook(cmd: str, tmp_path: Path, *, env_file: str | None = None,
                 extra_tool_input: dict | None = None,
                 extra_env: dict | None = None,
                 project_dir: str | None = None):
    home = tmp_path / "home"
    _make_fake_lean_ctx(home / ".cargo" / "bin")
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    # tests/conftest.py pins CLAUDE_PROJECT_DIR to a suite-wide scratch
    # project; these tests stage their OWN project cwd, so the pin must not
    # leak in (the explicit-project_dir arm below sets it deliberately).
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["HOME"] = str(home)
    env["PATH"] = "/usr/bin:/bin"
    env["FAKE_ARGV_LOG"] = str(home / "argv.log")
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    if extra_env:
        env.update(extra_env)
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    if env_file is not None:
        (proj / ".claude").mkdir(exist_ok=True)
        (proj / ".claude" / "env").write_text(env_file, encoding="utf-8")
    return subprocess.run(
        ["bash", str(SH_HOOK)],
        input=_payload(cmd, **(extra_tool_input or {})),
        capture_output=True, text=True, cwd=proj, env=env, timeout=30,
    )


def _run_ps1_hook(cmd: str, tmp_path: Path, *, env_file: str | None = None,
                  extra_tool_input: dict | None = None,
                  project_dir: str | None = None):
    bin_dir = tmp_path / "fakebin"
    _make_fake_lean_ctx(bin_dir)
    env = dict(os.environ)
    env.pop("VCT_DISABLE_HOOKS", None)
    # See _run_sh_hook: the conftest scratch-project pin must not leak in.
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_ARGV_LOG"] = str(tmp_path / "argv.log")
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    if env_file is not None:
        (proj / ".claude").mkdir(exist_ok=True)
        (proj / ".claude" / "env").write_text(env_file, encoding="utf-8")
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(PS1_HOOK)],
        input=_payload(cmd, **(extra_tool_input or {})),
        capture_output=True, text=True, cwd=proj, env=env, timeout=60,
    )


def _rawdir(proj: Path) -> Path:
    return proj / ".claude" / "state" / "lean-ctx-tee"


# ─── .sh hook behaviour ──────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hook; .ps1 behavioural cases are pwsh-gated below.",
)


class TestShAllowListGate:
    @pytest.mark.parametrize("cmd", WRAP_CASES)
    def test_allow_listed_command_wrapped(self, cmd, tmp_path):
        res = _run_sh_hook(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, f"allow-listed command must be wrapped: {cmd}"
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert "permissionDecision" not in hso, (
            "the hook constructs the response itself; an auto-approval "
            "field must never appear (D-3 invariant, now structural)"
        )
        wrapped = hso["updatedInput"]["command"]
        assert wrapped.startswith("bash "), wrapped
        assert "_lib/lean-ctx-tee.sh" in wrapped, wrapped
        # the original command text reaches the wrapper via a cmd file
        cmdfiles = list(_rawdir(tmp_path / "proj").glob("*.cmd"))
        assert len(cmdfiles) == 1, "exactly one cmd file must be written"
        assert cmdfiles[0].read_text(encoding="utf-8") == cmd

    @pytest.mark.parametrize("cmd", RAW_CASES)
    def test_everything_else_runs_raw(self, cmd, tmp_path):
        res = _run_sh_hook(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            f"non-allow-listed command must run raw, got rewrite: {cmd}"
        )
        assert not _rawdir(tmp_path / "proj").exists() or \
            not list(_rawdir(tmp_path / "proj").glob("*.cmd")), (
            f"no cmd file may be written for a raw command: {cmd}"
        )

    def test_wrapped_command_ends_with_default_ttl(self, tmp_path):
        res = _run_sh_hook("npm install", tmp_path)
        wrapped = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert wrapped.rstrip().split()[-1].strip("'") == "168", wrapped

    def test_ttl_knob_read_from_claude_env(self, tmp_path):
        res = _run_sh_hook("npm install", tmp_path,
                           env_file="VCO_LEAN_CTX_TEE_TTL_HOURS=24\n")
        wrapped = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert wrapped.rstrip().split()[-1].strip("'") == "24", wrapped

    def test_ttl_knob_invalid_falls_back_to_default(self, tmp_path):
        res = _run_sh_hook("npm install", tmp_path,
                           env_file="VCO_LEAN_CTX_TEE_TTL_HOURS=abc\n")
        wrapped = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert wrapped.rstrip().split()[-1].strip("'") == "168", wrapped

    def test_default_off_disables_wrapping(self, tmp_path):
        res = _run_sh_hook("npm install", tmp_path,
                           env_file="VCO_LEAN_CTX_DEFAULT=off\n")
        assert res.stdout.strip() == ""

    @pytest.mark.parametrize("val", ["Off", "OFF", "oFf"])
    def test_default_off_is_case_insensitive(self, val, tmp_path):
        """SF-3: `Off` in .claude/env (a hand edit) must disable compression
        on POSIX too — the .ps1 sibling and the launcher GUI mapping both
        compare case-insensitively; a case-sensitive .sh gate renders a
        launcher toggle that lies."""
        res = _run_sh_hook("npm install", tmp_path,
                           env_file=f"VCO_LEAN_CTX_DEFAULT={val}\n")
        assert res.stdout.strip() == "", (
            f"VCO_LEAN_CTX_DEFAULT={val} must disable compression"
        )

    def test_disable_hooks_short_circuits(self, tmp_path):
        res = _run_sh_hook("npm install", tmp_path,
                           extra_env={"VCT_DISABLE_HOOKS": "1"})
        assert res.stdout.strip() == ""

    def test_other_tool_input_fields_preserved(self, tmp_path):
        res = _run_sh_hook(
            "npm install", tmp_path,
            extra_tool_input={"description": "Install deps",
                              "run_in_background": True})
        ui = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]
        assert ui["description"] == "Install deps"
        assert ui["run_in_background"] is True

    def test_hook_never_invokes_the_binary_at_rewrite_time(self, tmp_path):
        """The pre-v0.2.101 delegation to the upstream rewrite handler is
        retired: the hook decides via the allow-list and constructs the
        response itself. The fake binary logs every invocation — the log
        must stay empty (only the wrapper, at tool-execution time, runs it).
        """
        res = _run_sh_hook("npm install", tmp_path)
        assert res.stdout.strip(), "wrap expected"
        argv_log = tmp_path / "home" / "argv.log"
        assert not argv_log.exists(), (
            f"the hook itself must not invoke lean-ctx: {argv_log.read_text()}"
        )

    def test_claude_project_dir_governs_tee_location(self, tmp_path):
        """Production path: Claude Code sets CLAUDE_PROJECT_DIR — the tee
        state dir resolves under it, not under an incidental cwd."""
        proj = tmp_path / "proj"
        res = _run_sh_hook("npm install", tmp_path, project_dir=str(proj))
        assert res.stdout.strip(), "wrap expected"
        assert len(list(_rawdir(proj).glob("*.cmd"))) == 1

    def test_hook_creates_private_dir_and_cmdfile(self, tmp_path):
        """SF-1: the hook-side tee dir is 0700 and the .cmd file 0600 at
        birth (no world-readable window for the command text)."""
        res = _run_sh_hook("npm install", tmp_path)
        assert res.stdout.strip(), "wrap expected"
        rawdir = _rawdir(tmp_path / "proj")
        assert stat.S_IMODE(rawdir.stat().st_mode) == 0o700
        (cf,) = rawdir.glob("*.cmd")
        assert stat.S_IMODE(cf.stat().st_mode) == 0o600

    def test_binary_absent_everywhere_clean_noop(self, tmp_path):
        home = tmp_path / "home"
        (home / "proj").mkdir(parents=True)
        env = dict(os.environ)
        env.pop("VCT_DISABLE_HOOKS", None)
        env["HOME"] = str(home)
        env["PATH"] = "/usr/bin:/bin"
        res = subprocess.run(
            ["bash", str(SH_HOOK)], input=_payload("npm install"),
            capture_output=True, text=True, cwd=home / "proj", env=env,
            timeout=30,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == ""


# ─── wrapper behaviour (.sh) ─────────────────────────────────────────────

def _run_wrapper_sh(cmd_text: str, tmp_path: Path, *, ttl: str = "168",
                    failing: bool = False):
    bin_dir = tmp_path / "fakebin"
    fake = _make_fake_lean_ctx(bin_dir, failing=failing)
    rawdir = tmp_path / "raw"
    rawdir.mkdir(parents=True, exist_ok=True)
    cmdfile = rawdir / "run.cmd"
    cmdfile.write_text(cmd_text, encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(TEE_SH), str(fake), str(rawdir), str(cmdfile), ttl],
        capture_output=True, text=True, cwd=tmp_path, env=env, timeout=60,
    )


_POINTER_RE = re.compile(
    r"\[lean-ctx-tee\] (\d+) raw lines -> (\d+) shown; full output: (\S+)")


class TestTeeWrapperSh:
    def test_compressed_run_writes_raw_file_and_pointer(self, tmp_path):
        res = _run_wrapper_sh("seq 1 120", tmp_path)
        assert res.returncode == 0, res.stderr
        assert "COMPRESSED-BY-FAKE" in res.stdout
        logs = list((tmp_path / "raw").glob("*.log"))
        assert len(logs) == 1, "the full raw output must be teed to a .log"
        assert logs[0].read_text(encoding="utf-8").splitlines() == [
            str(i) for i in range(1, 121)]
        m = _POINTER_RE.search(res.stdout)
        assert m, f"pointer line missing: {res.stdout!r}"
        assert m.group(1) == "120"
        assert m.group(3) == str(logs[0]), "pointer must name the tee file"
        # the pointer is the LAST line of the compressed output
        assert res.stdout.rstrip("\n").splitlines()[-1].startswith(
            "[lean-ctx-tee]")

    def test_exit_code_propagates(self, tmp_path):
        res = _run_wrapper_sh("echo out; exit 3", tmp_path)
        assert res.returncode == 3
        logs = list((tmp_path / "raw").glob("*.log"))
        assert "out" in logs[0].read_text(encoding="utf-8")

    def test_compressor_failure_falls_back_to_raw_output(self, tmp_path):
        """Never lose output: when lean-ctx fails, the raw text is printed
        and the pointer still names the tee file."""
        res = _run_wrapper_sh("seq 1 120", tmp_path, failing=True)
        assert res.returncode == 0, res.stderr
        assert "COMPRESSED-BY-FAKE" not in res.stdout
        for probe in ("1", "60", "120"):
            assert re.search(rf"^{probe}$", res.stdout, re.M), (
                f"raw fallback output missing line {probe!r}"
            )
        assert _POINTER_RE.search(res.stdout)

    def test_empty_output_still_gets_pointer(self, tmp_path):
        res = _run_wrapper_sh("true", tmp_path)
        assert res.returncode == 0, res.stderr
        logs = list((tmp_path / "raw").glob("*.log"))
        assert len(logs) == 1
        assert logs[0].read_text(encoding="utf-8") == ""
        m = _POINTER_RE.search(res.stdout)
        assert m and m.group(1) == "0"

    def test_unwritable_rawdir_runs_command_raw(self, tmp_path):
        """Leave-alone arm: no state dir -> no tee -> no compression; the
        command runs directly and its output passes through untouched."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        fake = _make_fake_lean_ctx(tmp_path / "fakebin")
        cmdfile = tmp_path / "run.cmd"
        cmdfile.write_text("seq 1 5", encoding="utf-8")
        res = subprocess.run(
            ["bash", str(TEE_SH), str(fake), str(blocker / "sub"),
             str(cmdfile), "168"],
            capture_output=True, text=True, cwd=tmp_path, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.split() == ["1", "2", "3", "4", "5"]
        assert "[lean-ctx-tee]" not in res.stdout

    def test_missing_cmdfile_fails_loudly(self, tmp_path):
        fake = _make_fake_lean_ctx(tmp_path / "fakebin")
        res = subprocess.run(
            ["bash", str(TEE_SH), str(fake), str(tmp_path / "raw"),
             str(tmp_path / "nope.cmd"), "168"],
            capture_output=True, text=True, cwd=tmp_path, timeout=30,
        )
        assert res.returncode != 0
        assert "nope.cmd" in res.stderr

    def test_ttl_sweep_deletes_stale_files(self, tmp_path):
        rawdir = tmp_path / "raw"
        rawdir.mkdir()
        old_log = rawdir / "old.log"
        old_cmd = rawdir / "old.cmd"
        fresh_log = rawdir / "fresh.log"
        for f in (old_log, old_cmd, fresh_log):
            f.write_text("x", encoding="utf-8")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old_log, (ten_days_ago, ten_days_ago))
        os.utime(old_cmd, (ten_days_ago, ten_days_ago))
        res = _run_wrapper_sh("true", tmp_path, ttl="168")
        assert res.returncode == 0, res.stderr
        assert not old_log.exists(), "stale .log must be swept at TTL 168h"
        assert not old_cmd.exists(), "stale .cmd must be swept at TTL 168h"
        assert fresh_log.exists(), "fresh files must survive the sweep"

    def test_ttl_zero_keeps_everything(self, tmp_path):
        rawdir = tmp_path / "raw"
        rawdir.mkdir()
        old_log = rawdir / "old.log"
        old_log.write_text("x", encoding="utf-8")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old_log, (ten_days_ago, ten_days_ago))
        res = _run_wrapper_sh("true", tmp_path, ttl="0")
        assert res.returncode == 0, res.stderr
        assert old_log.exists(), "TTL 0 = keep forever (no sweep)"

    def test_tee_artifacts_are_private(self, tmp_path):
        """SF-1: raw output of allow-listed curl/wget/test runs can carry
        credentials (SEC-RAW guards the COMMAND text, not the output), so
        the tee dir must be 0700 and the .log 0600 — even when the dir
        pre-existed world-traversable."""
        res = _run_wrapper_sh("seq 1 5", tmp_path)
        assert res.returncode == 0, res.stderr
        rawdir = tmp_path / "raw"
        assert stat.S_IMODE(rawdir.stat().st_mode) == 0o700, (
            "tee dir must be tightened to 0700"
        )
        (log,) = rawdir.glob("*.log")
        assert stat.S_IMODE(log.stat().st_mode) == 0o600, (
            "tee .log must be 0600 (raw output can contain credentials)"
        )

    # 17M two-byte chars = 34,000,000 BYTES but only 17,000,000 CHARS —
    # the size gate must count BYTES on both siblings (N-2).
    BIG_CMD = "python3 -c \"import sys; sys.stdout.write(chr(233) * 17000000)\""

    def test_oversized_output_counts_bytes_pointer_only(self, tmp_path):
        res = _run_wrapper_sh(self.BIG_CMD, tmp_path)
        assert res.returncode == 0, res.stderr
        assert "too large" in res.stdout, res.stdout[:200]
        m = re.search(r"too large to echo \((\d+) bytes\)", res.stdout)
        assert m and int(m.group(1)) >= 34_000_000, (
            f"the size arm must report BYTES (>= 34 MB): {res.stdout[:200]}"
        )
        assert len(res.stdout) < 5000, "oversized output must not be echoed"
        (log,) = (tmp_path / "raw").glob("*.log")
        assert log.stat().st_size == 34_000_000


# ─── static pins: shared data + retired machinery ────────────────────────

class TestSharedDataAndRetirements:
    def test_allowlist_file_is_the_single_source(self):
        assert ALLOWLIST.is_file()
        sh = SH_HOOK.read_text(encoding="utf-8")
        ps1 = PS1_HOOK.read_text(encoding="utf-8-sig")
        for src in (sh, ps1):
            assert "_lib/lean-ctx-allowlist.txt" in src or \
                "_lib\\lean-ctx-allowlist.txt" in src or \
                "lean-ctx-allowlist.txt" in src, (
                "both siblings must read the one committed allow-list file"
            )
            assert "MUST MATCH" in src

    def test_allowlist_seeds_progress_noise_and_test_build_runners(self):
        entries = [
            ln.split("#", 1)[0].strip()
            for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        ]
        entries = [e for e in entries if e]
        for expected in ("npm install", "pip install", "docker pull",
                         "wget", "curl", "pytest", "cargo build",
                         "cargo test", "vitest", "tsc"):
            assert expected in entries, f"missing allow-list entry: {expected}"

    def test_git_is_not_allow_listed(self):
        for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines():
            entry = ln.split("#", 1)[0].strip()
            assert not entry.startswith("git "), (
                "git must never be allow-listed (owner rule)"
            )

    def test_upstream_rewrite_delegation_retired(self):
        """The hooks construct updatedInput themselves; the delegation to
        lean-ctx's own rewrite handler (and its permissionDecision strip)
        is gone — the string must not survive anywhere in either sibling."""
        for hook in (SH_HOOK, PS1_HOOK):
            src = hook.read_text(encoding="utf-8-sig")
            assert "hook rewrite" not in src, (
                f"{hook.name} still references the retired delegation"
            )

    def test_trim_machinery_retired(self):
        for hook in (SH_HOOK, PS1_HOOK):
            src = hook.read_text(encoding="utf-8-sig")
            assert "GIT-READONLY-VERBS" not in src, (
                f"{hook.name}: TRIM-r verb list must be retired"
            )

    def test_sec_raw_kept_and_parity_pinned(self):
        """SEC-RAW stays (allow-listed installers/downloaders can carry
        credentials); the pattern block remains between the same markers in
        both siblings, byte-identical (extraction, not a source scan)."""
        def extract(src: str, quote: str) -> list[str]:
            begin = src.index("SEC-RAW-PATTERNS-BEGIN")
            end = src.index("SEC-RAW-PATTERNS-END")
            out = []
            for line in src[begin:end].splitlines():
                line = line.strip().rstrip(",")
                if quote == '"' and line.startswith('r"') and line.endswith('"'):
                    out.append(line[2:-1])
                elif quote == "'" and line.startswith("'") and line.endswith("'"):
                    out.append(line[1:-1])
            return out

        sh_patterns = extract(SH_HOOK.read_text(encoding="utf-8"), '"')
        ps1_patterns = extract(PS1_HOOK.read_text(encoding="utf-8-sig"), "'")
        assert sh_patterns, "sh SEC-RAW block missing or unparsed"
        assert sh_patterns == ps1_patterns, "SEC-RAW lists diverged"
        import re as _re
        for p in sh_patterns:
            _re.compile(p)
        # the v0.2.101 additions are present
        joined = "\n".join(sh_patterns)
        assert "://" in joined and "@" in joined, (
            "URL-userinfo credential pattern missing"
        )
        assert "auth" in joined.lower(), "_authToken pattern missing"


class TestBundleShipping:
    def test_lib_data_files_enumerate_into_the_bundle(self, tmp_path):
        """The allow-list + wrapper must SHIP: the bundle engine's _lib loop
        covers the data glob, not just .sh/.ps1 flavours."""
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.project_init import _enumerate_bundle_files

        orch = tmp_path / "orch"
        lib = orch / "templates" / "hooks" / "_lib"
        lib.mkdir(parents=True)
        (orch / "templates" / "hooks" / "lean-ctx-rewrite.sh").write_text(
            "#!/usr/bin/env bash\n", encoding="utf-8")
        (orch / "templates" / "hooks" / "lean-ctx-rewrite.ps1").write_text(
            "#\n", encoding="utf-8")
        for name in ("lean-ctx-allowlist.txt", "lean-ctx-tee.sh",
                     "lean-ctx-tee.ps1"):
            (lib / name).write_text("x\n", encoding="utf-8")

        ops = _enumerate_bundle_files(orch, tmp_path / "proj")
        by_dest = {op.dest_rel: op for op in ops}
        for name in ("lean-ctx-allowlist.txt", "lean-ctx-tee.sh",
                     "lean-ctx-tee.ps1"):
            dest = str(Path(".claude") / "hooks" / "_lib" / name)
            assert dest in by_dest, f"_lib/{name} must ship in the bundle"
            assert by_dest[dest].always_overwrite, (
                f"_lib/{name} must be always-overwrite (not user-customisable)"
            )

    def test_hook_lib_data_glob_declared(self):
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.bundle_globs import hook_lib_data_globs
        assert "*.txt" in hook_lib_data_globs()


class TestStateDirExclusions:
    """SF-1 confirmation pins: the tee dir sits in the two exclusion regimes
    the review asked to have named. (KG/docs sync needs no pin: the routing
    home `_lib/route-touched-path.sh` only routes knowledge/**, docs/**.md,
    diagrams and code extensions — `.claude/state/**` matches no route.)"""

    def test_git_exclude_covers_the_whole_claude_tree(self):
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.git_exclude import VCO_EXCLUSIVE_TOPLEVEL
        assert VCO_EXCLUSIVE_TOPLEVEL[".claude"] == "/.claude/", (
            "bundle add/update writes /.claude/ to .git/info/exclude — the "
            "tee dir must stay inside that exclusion"
        )

    def test_tee_paths_are_codegraph_purgeable(self):
        sys.path.insert(0, str(REPO_ROOT))
        from vco_lib.codegraph_row_classify import classify_row
        verdict = classify_row(
            {"file_path": ".claude/state/lean-ctx-tee/20260101T000000Z-1-ab.log"},
            None,
        )
        assert verdict == "purgeable", (
            f"tee paths must classify transient (got {verdict!r}) — the "
            "code graph never indexes .claude/state/"
        )


# ─── pwsh-gated .ps1 parity ──────────────────────────────────────────────

def _make_failing_chmod_shim(shim_dir: Path) -> Path:
    """A `chmod` that always fails, first on PATH — simulates a POSIX host
    where dir/file privacy cannot be established (NF-4)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "chmod"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir


@pytest.mark.skipif(not _HAS_PWSH, reason="pwsh not installed")
class TestPs1HookParity:
    @pytest.mark.parametrize("cmd", WRAP_CASES)
    def test_allow_listed_command_wrapped(self, cmd, tmp_path):
        res = _run_ps1_hook(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        out = res.stdout.strip()
        assert out, f"ps1: allow-listed command must be wrapped: {cmd}"
        data = json.loads(out)
        hso = data["hookSpecificOutput"]
        assert "permissionDecision" not in json.dumps(hso)
        wrapped = hso["updatedInput"]["command"]
        assert "lean-ctx-tee.ps1" in wrapped, wrapped
        cmdfiles = list(_rawdir(tmp_path / "proj").glob("*.cmd"))
        assert len(cmdfiles) == 1
        assert cmdfiles[0].read_text(encoding="utf-8") == cmd

    @pytest.mark.parametrize("cmd", RAW_CASES)
    def test_everything_else_runs_raw(self, cmd, tmp_path):
        res = _run_ps1_hook(cmd, tmp_path)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            f"ps1: non-allow-listed command must run raw: {cmd}"
        )

    def test_ttl_knob_and_default(self, tmp_path):
        res = _run_ps1_hook("npm install", tmp_path)
        wrapped = json.loads(
            res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert wrapped.rstrip().endswith("'168'"), wrapped
        res = _run_ps1_hook("npm install", tmp_path,
                            env_file="VCO_LEAN_CTX_TEE_TTL_HOURS=24\n")
        wrapped = json.loads(
            res.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        assert wrapped.rstrip().endswith("'24'"), wrapped

    def test_default_off_disables_wrapping(self, tmp_path):
        res = _run_ps1_hook("npm install", tmp_path,
                            env_file="VCO_LEAN_CTX_DEFAULT=off\n")
        assert res.stdout.strip() == ""

    def test_other_tool_input_fields_preserved(self, tmp_path):
        res = _run_ps1_hook(
            "npm install", tmp_path,
            extra_tool_input={"description": "Install deps"})
        ui = json.loads(res.stdout)["hookSpecificOutput"]["updatedInput"]
        assert ui["description"] == "Install deps"

    def test_hook_never_invokes_the_binary_at_rewrite_time(self, tmp_path):
        res = _run_ps1_hook("npm install", tmp_path)
        assert res.stdout.strip(), "wrap expected"
        argv_log = tmp_path / "argv.log"
        assert not argv_log.exists()

    def test_claude_project_dir_governs_tee_location(self, tmp_path):
        proj = tmp_path / "proj"
        res = _run_ps1_hook("npm install", tmp_path, project_dir=str(proj))
        assert res.stdout.strip(), "wrap expected"
        assert len(list(_rawdir(proj).glob("*.cmd"))) == 1

    @pytest.mark.parametrize("val", ["Off", "OFF"])
    def test_default_off_is_case_insensitive(self, val, tmp_path):
        """SF-3 parity: the .ps1 already lowercased; pinned so the pair
        stays symmetric with the .sh case-glob."""
        res = _run_ps1_hook("npm install", tmp_path,
                            env_file=f"VCO_LEAN_CTX_DEFAULT={val}\n")
        assert res.stdout.strip() == ""

    def test_hook_creates_private_dir_and_cmdfile(self, tmp_path):
        """SF-1 parity: on POSIX hosts (pwsh) the .ps1 hook tightens the
        tee dir to 0700 and the .cmd file to 0600; on native Windows the
        profile ACLs are the equivalent (documented in the hook)."""
        res = _run_ps1_hook("npm install", tmp_path)
        assert res.stdout.strip(), "wrap expected"
        rawdir = _rawdir(tmp_path / "proj")
        assert stat.S_IMODE(rawdir.stat().st_mode) == 0o700
        (cf,) = rawdir.glob("*.cmd")
        assert stat.S_IMODE(cf.stat().st_mode) == 0o600

    def test_chmod_failure_fails_closed_with_no_default_perm_artifact(self, tmp_path):
        """NF-4: when dir privacy cannot be ESTABLISHED (chmod fails on a
        POSIX host), the hook must fail closed — no rewrite, and never a
        cmd file sitting at default permissions. The .sh sibling gets this
        from os.chmod raising into the nothing() arm; the .ps1 must match.
        Red against the pre-NF-4 code, which chmod-ed best-effort AFTER a
        default-permission create and wrapped regardless."""
        shim = _make_failing_chmod_shim(tmp_path / "shim")
        bin_dir = tmp_path / "fakebin"
        _make_fake_lean_ctx(bin_dir)
        env = dict(os.environ)
        env.pop("VCT_DISABLE_HOOKS", None)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["PATH"] = f"{shim}{os.pathsep}{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        proj = tmp_path / "proj"
        proj.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(PS1_HOOK)],
            input=_payload("npm install"), capture_output=True, text=True,
            cwd=proj, env=env, timeout=60,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == "", (
            "unprovable dir privacy must fail closed (raw), not wrap"
        )
        rawdir = _rawdir(proj)
        for f in (rawdir.glob("*") if rawdir.exists() else []):
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, (
                f"no artifact may hold command text at default perms: {f}"
            )


@pytest.mark.skipif(not _HAS_PWSH, reason="pwsh not installed")
class TestTeeWrapperPs1:
    def _run(self, cmd_text: str, tmp_path: Path, *, ttl: str = "168",
             failing: bool = False):
        bin_dir = tmp_path / "fakebin"
        fake = _make_fake_lean_ctx(bin_dir, failing=failing)
        rawdir = tmp_path / "raw"
        rawdir.mkdir(parents=True, exist_ok=True)
        cmdfile = rawdir / "run.cmd"
        cmdfile.write_text(cmd_text, encoding="utf-8")
        env = dict(os.environ)
        return subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(TEE_PS1),
             str(fake), str(rawdir), str(cmdfile), ttl],
            capture_output=True, text=True, cwd=tmp_path, env=env,
            timeout=120,
        )

    def test_compressed_run_writes_raw_file_and_pointer(self, tmp_path):
        res = self._run("seq 1 120", tmp_path)
        assert res.returncode == 0, res.stderr
        assert "COMPRESSED-BY-FAKE" in res.stdout
        logs = list((tmp_path / "raw").glob("*.log"))
        assert len(logs) == 1
        assert logs[0].read_text(encoding="utf-8").split() == [
            str(i) for i in range(1, 121)]
        m = _POINTER_RE.search(res.stdout)
        assert m, f"ps1 pointer line missing: {res.stdout!r}"
        assert m.group(1) == "120"
        assert m.group(3) == str(logs[0])

    def test_exit_code_propagates_with_tee_and_pointer(self, tmp_path):
        """N-1: an in-process ScriptBlock let a command-text `exit N` kill
        the WHOLE wrapper before the tee write — the .sh sibling (child
        `bash -c`) never had that hole. The wrapper must run the command in
        a child process so `exit 3` still tees, points, and exits 3."""
        res = self._run("exit 3", tmp_path)
        assert res.returncode == 3
        logs = list((tmp_path / "raw").glob("*.log"))
        assert len(logs) == 1, (
            "tee must survive a command-text `exit N` (child-process run)"
        )
        assert _POINTER_RE.search(res.stdout), (
            f"pointer must survive a command-text `exit N`: {res.stdout!r}"
        )

    def test_native_exit_code_propagates(self, tmp_path):
        res = self._run("bash -c 'exit 4'", tmp_path)
        assert res.returncode == 4
        assert len(list((tmp_path / "raw").glob("*.log"))) == 1

    def test_compressor_failure_falls_back_to_raw_output(self, tmp_path):
        res = self._run("seq 1 120", tmp_path, failing=True)
        assert res.returncode == 0, res.stderr
        assert "COMPRESSED-BY-FAKE" not in res.stdout
        assert re.search(r"^60$", res.stdout, re.M), "raw fallback missing"
        assert _POINTER_RE.search(res.stdout)

    def test_tee_artifacts_are_private(self, tmp_path):
        """SF-1 parity (pwsh-on-POSIX arm): dir 0700, .log 0600."""
        res = self._run("seq 1 5", tmp_path)
        assert res.returncode == 0, res.stderr
        rawdir = tmp_path / "raw"
        assert stat.S_IMODE(rawdir.stat().st_mode) == 0o700
        (log,) = rawdir.glob("*.log")
        assert stat.S_IMODE(log.stat().st_mode) == 0o600

    def test_unwritable_rawdir_runs_command_raw(self, tmp_path):
        """Parity with the .sh leave-alone arm: no state dir -> no tee ->
        no compression; the command output passes through UNTOUCHED (a
        `$null = Invoke-TeeCommand` regression would swallow it)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        fake = _make_fake_lean_ctx(tmp_path / "fakebin")
        cmdfile = tmp_path / "run.cmd"
        cmdfile.write_text("seq 1 5", encoding="utf-8")
        res = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(TEE_PS1),
             str(fake), str(blocker / "sub"), str(cmdfile), "168"],
            capture_output=True, text=True, cwd=tmp_path, timeout=120,
        )
        assert res.returncode == 0, res.stderr
        assert res.stdout.split() == ["1", "2", "3", "4", "5"]
        assert "[lean-ctx-tee]" not in res.stdout

    def test_log_private_even_when_chmod_fails(self, tmp_path):
        """NF-4 parity with the .sh `( umask 077; : >file )` birth: on a
        POSIX host the .log must be 0600 FROM BIRTH — no dependency on a
        working chmod, no default-permission window. Red against the
        pre-NF-4 code (WriteAllText-empty at 0644, then best-effort
        chmod)."""
        shim = _make_failing_chmod_shim(tmp_path / "shim")
        fake = _make_fake_lean_ctx(tmp_path / "fakebin")
        rawdir = tmp_path / "raw"
        rawdir.mkdir()
        cmdfile = rawdir / "run.cmd"
        cmdfile.write_text("seq 1 5", encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
        res = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(TEE_PS1),
             str(fake), str(rawdir), str(cmdfile), "168"],
            capture_output=True, text=True, cwd=tmp_path, env=env,
            timeout=120,
        )
        assert res.returncode == 0, res.stderr
        assert "COMPRESSED-BY-FAKE" in res.stdout
        assert _POINTER_RE.search(res.stdout)
        (log,) = rawdir.glob("*.log")
        assert stat.S_IMODE(log.stat().st_mode) == 0o600, (
            "tee .log must be born 0600 without relying on chmod"
        )

    def test_oversized_output_counts_bytes_pointer_only(self, tmp_path):
        """N-2: the 32 MiB gate must count BYTES (UTF-8), not .NET string
        CHARS — 17M two-byte chars are 34 MB and must trip the gate."""
        big = "python3 -c \"import sys; sys.stdout.write(chr(233) * 17000000)\""
        res = self._run(big, tmp_path)
        assert res.returncode == 0, res.stderr
        assert "too large" in res.stdout, res.stdout[:200]
        # >= bound, not byte-exact: the PS pipeline capture normalizes a
        # trailing newline (+1 byte vs the .sh byte-exact tee) - the
        # documented cosmetic N-2 divergence. The gate counting CHARS
        # (17,000,001) instead of BYTES would never trip at all.
        m = re.search(r"too large to echo \((\d+) bytes\)", res.stdout)
        assert m and int(m.group(1)) >= 34_000_000, (
            f"the size arm must report BYTES (>= 34 MB): {res.stdout[:200]}"
        )
        assert len(res.stdout) < 5000, "oversized output must not be echoed"
        (log,) = (tmp_path / "raw").glob("*.log")
        assert log.stat().st_size >= 34_000_000

    def test_ttl_sweep_deletes_stale_files(self, tmp_path):
        rawdir = tmp_path / "raw"
        rawdir.mkdir()
        old_log = rawdir / "old.log"
        old_log.write_text("x", encoding="utf-8")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old_log, (ten_days_ago, ten_days_ago))
        res = self._run("true", tmp_path, ttl="168")
        assert res.returncode == 0, res.stderr
        assert not old_log.exists(), "ps1: stale .log must be swept"

    def test_ttl_zero_keeps_everything(self, tmp_path):
        rawdir = tmp_path / "raw"
        rawdir.mkdir()
        old_log = rawdir / "old.log"
        old_log.write_text("x", encoding="utf-8")
        ten_days_ago = time.time() - 10 * 86400
        os.utime(old_log, (ten_days_ago, ten_days_ago))
        res = self._run("true", tmp_path, ttl="0")
        assert res.returncode == 0, res.stderr
        assert old_log.exists(), "ps1: TTL 0 = keep forever"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
