# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.54 Track A1 (S-1..S-8) — secrets-surface regression guards.

Covers the instruction-surface truth contract (template Secrets section,
README claims), the vct CLI contract (shared-scope defaults, new
subcommands), the security-hook remediation signpost, and the bootstrap
envelope `secrets` block. The vct CLI checks run the REAL script in a
sandboxed VCT_SECRETS_DIR — no argv-shape mocks.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "ORCHESTRATOR-CLAUDE.md.template"
VCT = REPO_ROOT / "tools" / "vct-secrets" / "vct"
SECRETS_README = REPO_ROOT / "tools" / "vct-secrets" / "README.md"
MIGRATION = REPO_ROOT / "tools" / "vct-secrets" / "MIGRATION.md"
CRED_HELPER = REPO_ROOT / "tools" / "vct-secrets" / "git-credential-vct"


# ─── S-1: template Secrets section ──────────────────────────────────────


def test_template_has_secrets_section():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "## Secrets" in text, "template must ship a Secrets section (S-1)"
    # Core remediation surface named.
    assert "vct exec --secret" in text
    assert "vco_lib.agent_secrets" in text or "agent_secrets" in text
    assert "_README.md" in text


def test_template_retired_chmod600_pattern_gone():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "~/.your-secrets" not in text, (
        "the retired chmod-600 file pattern (v0.1.7) must not be "
        "recommended by the shipped template"
    )
    # Replacement points at the launcher GUI flow.
    assert "OnboardingWizard" in text


# ─── S-2/S-5: vct CLI contract (live subprocess) ────────────────────────


@pytest.fixture()
def vct_env(tmp_path):
    env = dict(os.environ)
    env["VCT_SECRETS_DIR"] = str(tmp_path / "store")
    return env


def _vct(env, *args, stdin: str | None = None):
    return subprocess.run(
        ["bash", str(VCT), *args],
        input=stdin, capture_output=True, text=True, env=env, timeout=30,
    )


def test_vct_get_and_exec_default_to_shared(vct_env, tmp_path):
    store = Path(vct_env["VCT_SECRETS_DIR"])
    (store / "shared").mkdir(parents=True)
    (store / "shared" / "MYKEY").write_text("shared-val")
    os.chmod(store / "shared" / "MYKEY", 0o600)

    cp = _vct(vct_env, "get", "--key", "MYKEY")
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "shared-val"

    cp = _vct(
        vct_env, "exec", "--secret", "MYKEY=OUT_VAR", "--",
        "bash", "-c", 'printf %s "$OUT_VAR"',
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout == "shared-val"


def test_vct_can_read_and_resolve(vct_env):
    store = Path(vct_env["VCT_SECRETS_DIR"])
    (store / "shared").mkdir(parents=True)
    (store / "shared" / "PROBE").write_text("v")
    os.chmod(store / "shared" / "PROBE", 0o600)

    cp = _vct(vct_env, "can-read", "--key", "PROBE")
    assert cp.returncode == 0 and cp.stdout == ""
    cp = _vct(vct_env, "can-read", "--key", "ABSENT")
    assert cp.returncode == 1

    cp = _vct(vct_env, "resolve", "--key", "PROBE")
    assert cp.returncode == 0
    assert cp.stdout.strip() == str(store / "shared" / "PROBE")
    assert "v" != cp.stdout.strip(), "resolve must print a path, not the value"
    cp = _vct(vct_env, "resolve", "--key", "ABSENT")
    assert cp.returncode == 2


# ─── v0.2.76 R4: hub-aware miss message (vct-get-hub-keychain-disjoint) ──────


@contextlib.contextmanager
def _stub_hub(state_dir, *, has_key: bool):
    """Spin a localhost stub of vct-hub's GET /projects/{id}/env?key=... and
    write hub.port + hub.token under `state_dir` so `vct` discovers it.

    `has_key=True` → 200 with the key present; False → 404 (project/key absent).
    Auth is checked loosely (any Bearer token accepted) — enough to exercise
    the CLI's probe path.
    """
    import http.server
    import threading

    token = "stub-hub-token-r4"

    class _H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if "Bearer " not in (self.headers.get("Authorization") or ""):
                self.send_response(401)
                self.end_headers()
                return
            if has_key and "/env" in self.path:
                body = b'{"MYGUIKEY": "kept-in-keychain"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir) / "hub.port").write_text(str(port))
    (Path(state_dir) / "hub.token").write_text(token)
    try:
        yield
    finally:
        srv.shutdown()
        t.join(timeout=5)


def test_vct_get_miss_points_at_resolver_when_hub_has_key(vct_env, tmp_path):
    """ACT: a GUI-saved key absent from the file store but present in the
    launcher keychain (served by the hub) → the miss message points at the
    resolver script and must NOT suggest `vct set` (which forks a copy)."""
    state_dir = tmp_path / "vct-state"
    env = dict(vct_env)
    env["VCT_STATE_DIR"] = str(state_dir)
    with _stub_hub(state_dir, has_key=True):
        cp = _vct(env, "get", "--project", "myproj", "--key", "MYGUIKEY", "--trusted")
    assert cp.returncode == 2, cp.stderr
    assert "vct_secrets_resolve.sh" in cp.stderr, cp.stderr
    assert "Fix: vct set" not in cp.stderr, (
        "must NOT suggest `vct set` when the keychain has the key (divergent copy)"
    )


def test_vct_get_miss_unchanged_when_hub_lacks_key(vct_env, tmp_path):
    """LEAVE-ALONE: a truly-absent key (hub 404s too) → the classic
    `vct set` hint, unchanged."""
    state_dir = tmp_path / "vct-state"
    env = dict(vct_env)
    env["VCT_STATE_DIR"] = str(state_dir)
    with _stub_hub(state_dir, has_key=False):
        cp = _vct(env, "get", "--project", "myproj", "--key", "NOSUCH", "--trusted")
    assert cp.returncode == 2, cp.stderr
    assert "Fix: vct set" in cp.stderr, cp.stderr
    assert "vct_secrets_resolve.sh" not in cp.stderr


def test_vct_get_miss_unchanged_when_hub_down(vct_env, tmp_path):
    """LEAVE-ALONE + no-hang: hub unreachable (no port/token files, and a
    dead port) → the classic message, bounded (curl --max-time)."""
    state_dir = tmp_path / "vct-state"
    env = dict(vct_env)
    env["VCT_STATE_DIR"] = str(state_dir)
    # Point at a closed port so the probe fails fast (curl connection refused).
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "hub.port").write_text("9")  # unlikely-to-listen low port
    (state_dir / "hub.token").write_text("irrelevant")
    import time as _time
    t0 = _time.time()
    cp = _vct(env, "get", "--project", "myproj", "--key", "NOSUCH", "--trusted")
    elapsed = _time.time() - t0
    assert cp.returncode == 2, cp.stderr
    assert "Fix: vct set" in cp.stderr, cp.stderr
    assert elapsed < 15, f"hub-down probe must be bounded, took {elapsed:.1f}s"


def test_vct_list_hides_readme(vct_env):
    store = Path(vct_env["VCT_SECRETS_DIR"])
    (store / "shared").mkdir(parents=True)
    (store / "shared" / "_README.md").write_text("# docs")
    (store / "shared" / "REALKEY").write_text("v")
    cp = _vct(vct_env, "list")
    assert "REALKEY" in cp.stdout
    assert "_README.md" not in cp.stdout


# ─── S-4: security-hook remediation signpost ────────────────────────────


def test_bash_security_credential_block_carries_remediation():
    sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))
    try:
        import bash_security
    finally:
        sys.path.pop(0)
    cred_cmd = "env | grep " + "SEC" + "RET"
    ok, reason = bash_security.check_command(cred_cmd)
    assert not ok
    assert "REMEDIATION" in reason
    assert "vct" in reason
    # Non-credential rules stay hint-free (no noise on rm-blocks).
    ok2, reason2 = bash_security.check_command("mkfs /dev/sda")
    assert not ok2
    assert "REMEDIATION" not in reason2


# ─── S-6: README/MIGRATION truth ────────────────────────────────────────


def test_secrets_readme_no_false_launcher_claim():
    text = SECRETS_README.read_text(encoding="utf-8")
    assert "The VCT Launcher will do all of the above" not in text
    assert "Two stores" in text, "README must explain keychain vs file store"
    mig = MIGRATION.read_text(encoding="utf-8")
    assert "this happened for you" not in mig


def test_git_credential_vct_default_pattern_is_generic():
    text = CRED_HELPER.read_text(encoding="utf-8")
    # Needle assembled at runtime so tree-level privacy scanners don't
    # flag the guard itself.
    personal_needle = "PROG" + "ETTI"
    assert personal_needle not in text, "personal machine layout must not ship"
    assert "$HOME/dev" in text, "docs + script must agree on $HOME/dev default"


# ─── S-3: shared _README template ships + install.py wires it ───────────


def test_shared_readme_template_ships():
    tpl = REPO_ROOT / "templates" / "vct-secrets-shared-readme.template"
    assert tpl.is_file()
    text = tpl.read_text(encoding="utf-8")
    assert "ghp_" in text and "github_pat_" in text, (
        "key-shape table is the load-bearing content (S-3)"
    )
    install_py = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    assert "_materialize_vct_secrets_shared_readme" in install_py


# ─── S-8: bootstrap envelope secrets block (live) ───────────────────────


def _build_envelope(extra_env: dict | None = None) -> dict:
    env = dict(os.environ)
    env["VCT_BOOTSTRAP_TEST_MODE"] = "1"
    if extra_env:
        env.update(extra_env)
    cp = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install.py"), "--bootstrap", "--json"],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert cp.returncode == 0, cp.stderr
    return json.loads(cp.stdout)


def test_envelope_secrets_block_names_only(tmp_path):
    store = tmp_path / "store"
    (store / "shared").mkdir(parents=True)
    (store / "shared" / "tok_a").write_text("SUPER-SENSITIVE-VALUE")
    (store / "shared" / "_README.md").write_text("# doc")
    envelope = _build_envelope({"VCT_SECRETS_DIR": str(store)})
    s = envelope["secrets"]
    assert s["primitive"] == "vct-secrets"
    assert s["store_dir"] == str(store)
    assert s["shared_keys_available"] == ["tok_a"], (
        "_README.md must not be listed as a key"
    )
    raw = json.dumps(envelope)
    assert "SUPER-SENSITIVE-VALUE" not in raw, "values must NEVER enter the envelope"


def test_envelope_secrets_block_soft_fails_on_missing_store(tmp_path):
    envelope = _build_envelope({"VCT_SECRETS_DIR": str(tmp_path / "nope")})
    s = envelope["secrets"]
    assert s["store_dir_exists"] is False
    assert s["shared_keys_available"] == []


# ─── A1 install hygiene: query_logger no longer writes into package dir ─


def test_query_logger_writes_outside_package_dir(tmp_path):
    """v0.2.54 A1 hygiene: JSONL logs go to the state dir (or the
    VCT_QUERY_LOG_DIR override), never into the installed package
    directory. Run in a subprocess so module-level path resolution sees
    the env var."""
    code = (
        "from query_logger import QueryLogger, LOG_DIR, QUERY_LOG\n"
        "import pathlib, sys\n"
        "QueryLogger.log_search(query='q', collection='C')\n"
        "print(LOG_DIR)\n"
        "assert QUERY_LOG.is_file(), 'log file not written'\n"
    )
    env = dict(os.environ)
    env["VCT_QUERY_LOG_DIR"] = str(tmp_path / "qlogs")
    pkg_dir = REPO_ROOT / "claude_mcp_servers" / "weaviate_mcp"
    env["PYTHONPATH"] = str(pkg_dir)
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == str(tmp_path / "qlogs")
    # Nothing landed in the package dir.
    assert not list(pkg_dir.glob("*_queries.jsonl")), (
        "query log leaked into the package directory"
    )
