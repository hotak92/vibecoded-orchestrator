# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 WP-D item 4 — stale-env hub-token fallback PARITY across mirrors.

The decision "may this provably-refused request be retried once with the
on-disk token?" exists in EIGHT places (one per resolver surface). The
Python implementation is the SSOT; the others are thin mirrors, because a
per-call subprocess into Python is not reachable from a bash ``curl``
retry path (rule A>B>C: this is a C-mirror, so it gets a parity test).

This file drives the SAME synthetic fixtures through the four shipped
script mirrors (the F-8 quadruplet: ``vct_secrets_resolve.{sh,ps1}`` +
``vct_project_config.{sh,ps1}``) and the Python SSOT, and asserts the
four rules land identically:

  1. ``VCT_HUB_TOKEN_STRICT=1``           → no fallback (hermeticity pin)
  2. ``VCT_HUB_TOKEN`` unset/empty        → no fallback
  3. no readable on-disk token            → no fallback
  4. on-disk token == env token           → no fallback
  otherwise                               → the on-disk token, scoped
                                            first when a project id is given

It also pins the ONE definitive stderr sentence byte-identically in all
EIGHT mirrors (the two Python surfaces, the four scripts, the Rust CLI,
and the file-store ``vct``), so a reworded copy in one language cannot
drift away from the others.

The ps1 subset gates on a PowerShell runtime being on PATH (mirrors
``tests/test_resolver_corrupt_discovery_inputs.py``). All fixtures are
synthetic — no project-identifying strings, no real secrets.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vco_lib import project_config


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "templates" / "scripts"

BASH_MIRRORS = {
    "vct_secrets_resolve.sh": SCRIPTS / "vct_secrets_resolve.sh",
    "vct_project_config.sh": SCRIPTS / "vct_project_config.sh",
}
PS1_MIRRORS = {
    "vct_secrets_resolve.ps1": SCRIPTS / "vct_secrets_resolve.ps1",
    "vct_project_config.ps1": SCRIPTS / "vct_project_config.ps1",
}

#: The access-matrix gate client — the same fallback, but GLOBAL-token
#: only (``/projects/{id}/access/{collection}`` is not a
#: per-project-token route), so it takes no project id.
ACCESS_MIRRORS = {
    "vct_access_check.sh": SCRIPTS / "vct_access_check.sh",
    "vct_access_check.ps1": SCRIPTS / "vct_access_check.ps1",
}

#: Every file that carries the definitive line. The Python SSOT's
#: constant is the reference value.
MESSAGE_MIRRORS = [
    REPO_ROOT / "vco_lib" / "project_config.py",
    REPO_ROOT / "vco_lib" / "access_resolver.py",
    REPO_ROOT / "vco_lib" / "cli" / "verify_diagrams.py",
    REPO_ROOT / "vco_lib" / "codegraph_resync.py",
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py",
    REPO_ROOT / "claude_mcp_servers" / "wrappers" / "_base.py",
    SCRIPTS / "vct_secrets_resolve.sh",
    SCRIPTS / "vct_secrets_resolve.ps1",
    SCRIPTS / "vct_project_config.sh",
    SCRIPTS / "vct_project_config.ps1",
    SCRIPTS / "vct_access_check.sh",
    SCRIPTS / "vct_access_check.ps1",
    REPO_ROOT / "launcher" / "tools" / "vct-cli" / "src" / "main.rs",
    REPO_ROOT / "tools" / "vct-secrets" / "vct",
]

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

ENV_TOKEN = "stale-env-token-0000-not-a-real-secret"
DISK_TOKEN = "fresh-disk-token-1111-not-a-real-secret"
SCOPED_TOKEN = "scoped-disk-token-2222-not-a-real-secret"
PROJECT_ID = "11111111-2222-3333-4444-555555555555"

#: The literal used by every mirror to spell the strict guard.
STRICT_ENV = "VCT_HUB_TOKEN_STRICT"


def _seed_state(*, disk: bool = True, scoped: bool = False) -> str:
    state = tempfile.mkdtemp(prefix="vct-stale-token-test-")
    if disk:
        (Path(state) / "hub.token").write_text(DISK_TOKEN, encoding="utf-8")
    if scoped:
        (Path(state) / f"hub.token.{PROJECT_ID}").write_text(
            SCOPED_TOKEN, encoding="utf-8"
        )
    return state


def _bash_library(path: Path) -> str:
    """The script with its ``main "$@"`` entry stripped, so the helpers
    can be sourced without running the CLI (established F-8 pattern)."""
    src = path.read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines() if ln.strip() != 'main "$@"']
    fd, tmp = tempfile.mkstemp(prefix="vct-stale-lib-", suffix=".sh")
    os.close(fd)
    Path(tmp).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp


def _bash_library_truncated(path: Path, marker: str) -> str:
    """The script cut at ``marker``, for LINEAR scripts (no ``main``
    function) whose tail would perform the real request on source.
    Everything above the marker is discovery + helper definitions."""
    src = path.read_text(encoding="utf-8")
    idx = src.find(marker)
    body = src[:idx] if idx != -1 else src
    fd, tmp = tempfile.mkstemp(prefix="vct-stale-lib-", suffix=".sh")
    os.close(fd)
    Path(tmp).write_text(body, encoding="utf-8")
    return tmp


def _ps1_library(path: Path) -> str:
    """The script with the ``[CmdletBinding()] param(...)`` header and the
    ``# ── Main`` tail removed, so it can be dot-sourced (F-8 pattern)."""
    src = path.read_text(encoding="utf-8")
    cb_idx = src.find("[CmdletBinding")
    if cb_idx != -1:
        after = src.find("\n)\n", cb_idx)
        if after != -1:
            src = src[:cb_idx] + src[after + len("\n)\n"):]
    marker = "# ── Main"
    idx = src.find(marker)
    body = src[:idx] if idx != -1 else src
    fd, tmp = tempfile.mkstemp(prefix="vct-stale-lib-", suffix=".ps1")
    os.close(fd)
    # Preserve the UTF-8 BOM the originals ship with (BOM discipline).
    Path(tmp).write_text("﻿" + body, encoding="utf-8")
    return tmp


def _run_bash(script: Path, snippet: str, *, env_extra: dict, state: str):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp",
        "VCT_STATE_DIR": state,
        # Force every rate-limited warning through so the observation is
        # deterministic (vct_project_config.sh's stderr policy).
        "VCO_HOOK_DEBUG": "1",
    }
    env.update(env_extra)
    lib = _bash_library(script)
    try:
        return subprocess.run(
            ["bash", "-c", f'source "{lib}"; {snippet}'],
            env=env, capture_output=True, text=True, timeout=20,
        )
    finally:
        try:
            os.unlink(lib)
        except OSError:
            pass


def _run_ps1(script: Path, snippet: str, *, env_extra: dict, state: str):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "VCT_STATE_DIR": state,
        "VCO_HOOK_DEBUG": "1",
    }
    env.update(env_extra)
    lib = _ps1_library(script)
    try:
        return subprocess.run(
            [_PWSH, "-NoProfile", "-NonInteractive", "-Command",
             f'. "{lib}"; {snippet}'],
            env=env, capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.unlink(lib)
        except OSError:
            pass


# ─── The four rules, driven through every mirror ────────────────────────


class BashDecisionParityTest(unittest.TestCase):
    """`hub_stale_env_fallback_token` in both bash mirrors."""

    SNIPPET = (
        'if out=$(hub_stale_env_fallback_token "${1:-}"); '
        'then printf "FALLBACK:%s" "$out"; else printf "NONE"; fi'
    )

    def _decide(self, script: Path, *, env_extra: dict, state: str,
                project_id: str = "") -> str:
        r = _run_bash(
            script,
            f'set -- "{project_id}"; {self.SNIPPET}',
            env_extra=env_extra, state=state,
        )
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        return r.stdout.strip()

    def test_stale_pin_yields_the_disk_token(self) -> None:
        state = _seed_state()
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(script, env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state),
                    f"FALLBACK:{DISK_TOKEN}",
                )
        shutil.rmtree(state, ignore_errors=True)

    def test_strict_guard_disables_it(self) -> None:
        state = _seed_state()
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(
                        script,
                        env_extra={"VCT_HUB_TOKEN": ENV_TOKEN, STRICT_ENV: "1"},
                        state=state,
                    ),
                    "NONE",
                )
        shutil.rmtree(state, ignore_errors=True)

    def test_no_env_pin_no_fallback(self) -> None:
        state = _seed_state()
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(script, env_extra={}, state=state), "NONE"
                )
        shutil.rmtree(state, ignore_errors=True)

    def test_identical_tokens_no_fallback(self) -> None:
        state = _seed_state()
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(script, env_extra={"VCT_HUB_TOKEN": DISK_TOKEN},
                                 state=state),
                    "NONE",
                )
        shutil.rmtree(state, ignore_errors=True)

    def test_absent_disk_token_no_fallback(self) -> None:
        state = _seed_state(disk=False)
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(script, env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state),
                    "NONE",
                )
        shutil.rmtree(state, ignore_errors=True)

    def test_scoped_token_preferred_for_a_project_route(self) -> None:
        state = _seed_state(scoped=True)
        for name, script in BASH_MIRRORS.items():
            with self.subTest(mirror=name):
                self.assertEqual(
                    self._decide(script, env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state, project_id=PROJECT_ID),
                    f"FALLBACK:{SCOPED_TOKEN}",
                )
                # No project id → the GLOBAL token, same as the SSOT.
                self.assertEqual(
                    self._decide(script, env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state),
                    f"FALLBACK:{DISK_TOKEN}",
                )
        shutil.rmtree(state, ignore_errors=True)


@unittest.skipIf(_PWSH is None, "no pwsh/powershell on PATH")
class PowerShellDecisionParityTest(unittest.TestCase):
    """`Get-StaleEnvFallbackToken` in both ps1 mirrors."""

    def _decide(self, script: Path, *, env_extra: dict, state: str,
                project_id: str = "") -> str:
        snippet = (
            f"$t = Get-StaleEnvFallbackToken -ProjectId '{project_id}'; "
            "if ($null -eq $t) { [Console]::Out.Write('NONE') } "
            "else { [Console]::Out.Write('FALLBACK:' + $t) }"
        )
        r = _run_ps1(script, snippet, env_extra=env_extra, state=state)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        return r.stdout.strip()

    def test_rules_match_the_ssot(self) -> None:
        state = _seed_state(scoped=True)
        try:
            for name, script in PS1_MIRRORS.items():
                with self.subTest(mirror=name):
                    self.assertEqual(
                        self._decide(script,
                                     env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                     state=state),
                        f"FALLBACK:{DISK_TOKEN}",
                    )
                    self.assertEqual(
                        self._decide(script,
                                     env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                     state=state, project_id=PROJECT_ID),
                        f"FALLBACK:{SCOPED_TOKEN}",
                    )
                    self.assertEqual(
                        self._decide(
                            script,
                            env_extra={"VCT_HUB_TOKEN": ENV_TOKEN,
                                       STRICT_ENV: "1"},
                            state=state),
                        "NONE",
                    )
                    self.assertEqual(
                        self._decide(script, env_extra={}, state=state), "NONE"
                    )
                    self.assertEqual(
                        self._decide(script,
                                     env_extra={"VCT_HUB_TOKEN": DISK_TOKEN},
                                     state=state),
                        "NONE",
                    )
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_absent_disk_token_no_fallback(self) -> None:
        state = _seed_state(disk=False)
        try:
            for name, script in PS1_MIRRORS.items():
                with self.subTest(mirror=name):
                    self.assertEqual(
                        self._decide(script,
                                     env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                     state=state),
                        "NONE",
                    )
        finally:
            shutil.rmtree(state, ignore_errors=True)


class AccessCheckDecisionParityTest(unittest.TestCase):
    """The access-matrix gate client (bash + ps1) — same four rules.

    Two documented divergences from the resolver quadruplet, both
    deliberate: the fn takes NO project id, and a scoped
    ``hub.token.<id>`` is IGNORED, because
    ``/projects/{id}/access/{collection}`` is not a per-project-token
    route (presenting a scoped token there would itself 401).
    """

    BASH_CUT = "# ── Token required"

    def _decide_bash(self, *, env_extra: dict, state: str) -> str:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": "/tmp",
            "VCT_STATE_DIR": state,
            "VCO_HOOK_DEBUG": "1",
        }
        env.update(env_extra)
        lib = _bash_library_truncated(ACCESS_MIRRORS["vct_access_check.sh"],
                                      self.BASH_CUT)
        try:
            snippet = (
                'if out=$(stale_env_fallback_token); '
                'then printf "FALLBACK:%s" "$out"; else printf "NONE"; fi'
            )
            # Two positional args satisfy the script's own usage check.
            r = subprocess.run(
                ["bash", "-c", f'set -- p1 KG; source "{lib}"; {snippet}'],
                env=env, capture_output=True, text=True, timeout=20,
            )
        finally:
            try:
                os.unlink(lib)
            except OSError:
                pass
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        return r.stdout.strip()

    def _decide_ps1(self, *, env_extra: dict, state: str) -> str:
        snippet = (
            "$t = Get-AccessStaleEnvFallbackToken; "
            "if ($null -eq $t -or $t -eq '') { [Console]::Out.Write('NONE') } "
            "else { [Console]::Out.Write('FALLBACK:' + $t) }"
        )
        r = _run_ps1(ACCESS_MIRRORS["vct_access_check.ps1"], snippet,
                     env_extra=env_extra, state=state)
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        return r.stdout.strip()

    def test_bash_rules_match_the_ssot(self) -> None:
        state = _seed_state(scoped=True)
        try:
            self.assertEqual(
                self._decide_bash(env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                  state=state),
                f"FALLBACK:{DISK_TOKEN}",
                "the GLOBAL on-disk token is used — never the scoped file",
            )
            self.assertEqual(
                self._decide_bash(
                    env_extra={"VCT_HUB_TOKEN": ENV_TOKEN, STRICT_ENV: "1"},
                    state=state),
                "NONE",
            )
            self.assertEqual(
                self._decide_bash(env_extra={}, state=state), "NONE"
            )
            self.assertEqual(
                self._decide_bash(env_extra={"VCT_HUB_TOKEN": DISK_TOKEN},
                                  state=state),
                "NONE",
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_bash_absent_disk_token_no_fallback(self) -> None:
        state = _seed_state(disk=False, scoped=True)
        try:
            self.assertEqual(
                self._decide_bash(env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                  state=state),
                "NONE",
                "a scoped token must NOT rescue a global-token route",
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)

    @unittest.skipIf(_PWSH is None, "no pwsh/powershell on PATH")
    def test_ps1_rules_match_the_ssot(self) -> None:
        state = _seed_state(scoped=True)
        try:
            self.assertEqual(
                self._decide_ps1(env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state),
                f"FALLBACK:{DISK_TOKEN}",
            )
            self.assertEqual(
                self._decide_ps1(
                    env_extra={"VCT_HUB_TOKEN": ENV_TOKEN, STRICT_ENV: "1"},
                    state=state),
                "NONE",
            )
            self.assertEqual(
                self._decide_ps1(env_extra={}, state=state), "NONE"
            )
            self.assertEqual(
                self._decide_ps1(env_extra={"VCT_HUB_TOKEN": DISK_TOKEN},
                                 state=state),
                "NONE",
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)

    @unittest.skipIf(_PWSH is None, "no pwsh/powershell on PATH")
    def test_ps1_absent_disk_token_no_fallback(self) -> None:
        state = _seed_state(disk=False, scoped=True)
        try:
            self.assertEqual(
                self._decide_ps1(env_extra={"VCT_HUB_TOKEN": ENV_TOKEN},
                                 state=state),
                "NONE",
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)


class PythonAccessResolverParityTest(unittest.TestCase):
    """`vco_lib/access_resolver.py` — the Python leg of the access-gate
    trio. Same four rules; GLOBAL token only (no scoped branch), matching
    its own bash/ps1 siblings."""

    def test_rules(self) -> None:
        from vco_lib import access_resolver

        state = _seed_state(scoped=True)
        try:
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": state,
                                              "VCT_HUB_TOKEN": ENV_TOKEN}):
                os.environ.pop(STRICT_ENV, None)
                self.assertEqual(
                    access_resolver._stale_env_token_fallback(),
                    DISK_TOKEN,
                    "the GLOBAL on-disk token is used — never the scoped file",
                )
                with mock.patch.dict(os.environ, {STRICT_ENV: "1"}):
                    self.assertIsNone(access_resolver._stale_env_token_fallback())
                with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
                    self.assertIsNone(access_resolver._stale_env_token_fallback())
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": state}, clear=True):
                self.assertIsNone(access_resolver._stale_env_token_fallback())
        finally:
            shutil.rmtree(state, ignore_errors=True)

    def test_absent_disk_token_no_fallback(self) -> None:
        from vco_lib import access_resolver

        state = _seed_state(disk=False, scoped=True)
        try:
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": state,
                                              "VCT_HUB_TOKEN": ENV_TOKEN}):
                os.environ.pop(STRICT_ENV, None)
                self.assertIsNone(
                    access_resolver._stale_env_token_fallback(),
                    "a scoped token must NOT rescue a global-token route",
                )
        finally:
            shutil.rmtree(state, ignore_errors=True)


class PythonSsotTest(unittest.TestCase):
    """The reference behaviour the mirrors above are compared against."""

    def test_rules(self) -> None:
        state = _seed_state(scoped=True)
        try:
            with mock.patch.dict(os.environ, {"VCT_STATE_DIR": state,
                                              "VCT_HUB_TOKEN": ENV_TOKEN}):
                os.environ.pop(STRICT_ENV, None)
                self.assertEqual(
                    project_config._stale_env_token_fallback(), DISK_TOKEN
                )
                self.assertEqual(
                    project_config._stale_env_token_fallback(PROJECT_ID),
                    SCOPED_TOKEN,
                )
                with mock.patch.dict(os.environ, {STRICT_ENV: "1"}):
                    self.assertIsNone(project_config._stale_env_token_fallback())
                with mock.patch.dict(os.environ, {"VCT_HUB_TOKEN": DISK_TOKEN}):
                    self.assertIsNone(project_config._stale_env_token_fallback())
        finally:
            shutil.rmtree(state, ignore_errors=True)


# ─── The definitive line is byte-identical in all eight mirrors ─────────


def _normalise_source(text: str) -> str:
    """Undo the three ways a source file may SPLIT or ESCAPE the literal.

    The sentence itself must be identical everywhere; how each language
    spells a long string literal must not be. We therefore join Python's
    implicit adjacent-string concatenation, join backslash-newline
    continuations (Rust), and unescape bash's ``\\```-escaped backticks
    before looking for the reference sentence.
    """
    text = re.sub(r'"\s*\n\s*"', "", text)      # "abc" \n "def"  → "abcdef"
    text = re.sub(r"\\\s*\n\s*", "", text)      # "abc \<nl> def" → "abc def"
    text = text.replace("\\`", "`")             # bash \` → `
    return text


#: The ADOPT GATE each mirror must spell (v0.2.91 wave-3, MINOR-1). The
#: rule is uniform — a retry's answer is adopted, and the env pin latched
#: off, ONLY when it proves the fallback credential was accepted: a 2xx, or
#: a 404 (which the hub answers strictly AFTER its auth middleware accepted
#: the bearer, so it is a post-auth answer just like a 200). This table
#: pins the SHAPE per language so a mirror cannot quietly widen back to
#: "anything that is not 401/403", which is what let a 5xx hiccup rewrite a
#: truthful `hub_auth_401` into `hub_5xx_503`.
ADOPT_GATE_MIRRORS = {
    REPO_ROOT / "vco_lib" / "project_config.py":
        ["def _retry_answer_is_definitive", "200 <= status_code < 300",
         "status_code == 404"],
    REPO_ROOT / "vco_lib" / "access_resolver.py":
        ["def _retry_answer_is_definitive", "attempt.code == 404"],
    REPO_ROOT / "claude_mcp_servers" / "wrappers" / "_base.py":
        ["def _retry_answer_is_definitive", "200 <= status < 300",
         "status == 404"],
    REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp" / "server.py":
        ["def _retry_answer_is_definitive", "200 <= status < 300",
         "status == 404"],
    SCRIPTS / "vct_secrets_resolve.sh": ["2??|404)"],
    SCRIPTS / "vct_project_config.sh": ["2??|404)"],
    SCRIPTS / "vct_access_check.sh": ["2??|404)"],
    REPO_ROOT / "tools" / "vct-secrets" / "vct": ["2??|404)"],
    SCRIPTS / "vct_secrets_resolve.ps1":
        ["Test-StaleEnvRetryIsDefinitive", "$Status -eq 404"],
    SCRIPTS / "vct_project_config.ps1":
        ["Test-StaleEnvRetryIsDefinitive", "$Status -eq 404"],
    SCRIPTS / "vct_access_check.ps1":
        ["Test-AccessStaleEnvRetryIsDefinitive", "$Status -eq 404"],
    REPO_ROOT / "launcher" / "tools" / "vct-cli" / "src" / "main.rs":
        ["fn retry_answer_is_definitive", "status.is_success()",
         "as_u16() == 404"],
}


class AdoptGateParityTest(unittest.TestCase):
    def test_every_mirror_adopts_only_a_definitive_answer(self) -> None:
        for path, needles in ADOPT_GATE_MIRRORS.items():
            with self.subTest(mirror=path.name):
                text = path.read_text(encoding="utf-8")
                for needle in needles:
                    self.assertIn(
                        needle, text,
                        f"{path} must gate the stale-env retry's adoption on "
                        f"a DEFINITIVE answer (2xx or 404)",
                    )

    def test_no_mirror_still_adopts_on_bare_not_refused(self) -> None:
        """The pre-fix shape, spelled in each language. Any of these
        reappearing means a mirror widened back to "adopt anything that is
        not 401/403" — the exact regression MINOR-1 closed."""
        forbidden = {
            SCRIPTS / "vct_secrets_resolve.sh": "401|403) : ;;",
            SCRIPTS / "vct_project_config.sh": "401|403) : ;;",
            SCRIPTS / "vct_access_check.sh": "401|403) : ;;",
            REPO_ROOT / "tools" / "vct-secrets" / "vct": "403|401) : ;;",
            REPO_ROOT / "launcher" / "tools" / "vct-cli" / "src" / "main.rs":
                "if is_auth_refusal(retry_status)",
        }
        for path, needle in forbidden.items():
            with self.subTest(mirror=path.name):
                self.assertNotIn(
                    needle, path.read_text(encoding="utf-8"),
                    f"{path} adopts a retry answer that proves nothing",
                )


class MessageParityTest(unittest.TestCase):
    def test_every_mirror_carries_the_same_sentence(self) -> None:
        reference = project_config.STALE_ENV_TOKEN_MESSAGE
        # Sanity: the sentence names both the symptom and the fix.
        self.assertIn("VCT_HUB_TOKEN", reference)
        self.assertIn("unset VCT_HUB_TOKEN", reference)
        for path in MESSAGE_MIRRORS:
            with self.subTest(mirror=path.name):
                normalised = _normalise_source(
                    path.read_text(encoding="utf-8")
                )
                self.assertIn(
                    reference, normalised,
                    f"{path} must carry the definitive line verbatim",
                )

    def test_every_mirror_spells_the_strict_guard(self) -> None:
        for path in MESSAGE_MIRRORS:
            with self.subTest(mirror=path.name):
                self.assertIn(
                    STRICT_ENV, path.read_text(encoding="utf-8"),
                    f"{path} must honour the {STRICT_ENV} hermeticity guard",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
