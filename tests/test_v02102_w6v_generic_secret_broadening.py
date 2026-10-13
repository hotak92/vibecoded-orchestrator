# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-V — the generic-secret shape covers short quoted literals.

Owner ruling 2026-10-12 (W6-T audit answers): "secrets may not just be keys,
but also passwords, ports, IPs, whatever, potentially shorter than 120
chars". So, in the ``content_scan`` context only:

* the key list gains PASSWORD / PASSWD / PWD / PASSPHRASE (compounds such
  as ``DB_PASSWORD`` through the existing unanchored key match), and every
  key matches in any letter case;
* a QUOTED literal value of any non-empty length alerts (no 32-char floor);
* a JS backtick literal (no ``${``) is its own code-only shape: in an
  env/config-style file a backtick is shell command substitution (syntax);
* references and expressions are not literals: ``getenv(...)``,
  ``os.environ[...]``, ``$PASS``, ``"${...}"``, ``"$PASS"``, ``"{...}"``
  format templates, f-strings and empty strings stay silent; nothing
  about a VALUE is judged, so a ``"<YOUR_KEY>"`` placeholder alerts;
* the UNQUOTED shape is decided BY FILE KIND (owner ruling 2026-10-13,
  review F2; ``vco_lib.credential_shapes.file_kind``): in an env/config-style
  file (.env*, .ini, .properties, .conf/.cfg, shell, YAML, Dockerfile) ANY
  non-empty unquoted value alerts except a ``$`` reference; in a code file an
  unquoted value is an expression and is not matched; there is no length
  floor;
* the explicit marker ``vco-secret-scan: test-placeholder`` silences its own
  line, as for every other shape;
* ``command_scan`` / ``repo_scan`` are NOT broadened.

Every scanner implementation runs for real and must agree: the bash
reconciler scanner (``_lib/credscan.sh``), its PowerShell sibling
(``_lib/credscan.ps1``, which must match CASE-SENSITIVELY like grep and
Python -- ``-match`` is case-insensitive in .NET), and post-tool-security
(``.sh``/``.ps1`` both run ``credscan_introduced.py``, Python ``re``).

Texts are assembled from parts so no line of THIS file carries a
credential-shaped literal; no test prints a value.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from vco_lib.credential_shapes import (
    TEST_PLACEHOLDER_MARKER,
    matching_labels,
    patterns_for_context,
)

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "templates" / "hooks" / "_lib"
HOOKS = REPO / "templates" / "hooks"

CODE = "app.py"  # a code-kind path: the unquoted shape does not apply
Q = "Generic secret"
U = "Generic secret (unquoted)"
BT = "Generic secret (backtick literal)"
UY = "Generic secret (unquoted, YAML)"
GEN = (Q, U, BT, UY)
EQ = " = "


def _assign(key: str, value: str, quote: str = '"', sep: str = EQ) -> str:
    return key + sep + quote + value + quote


# (name, text, expected generic labels)
ALERTS = [
    ("password_hunter2", _assign("password", "hunter" + "2"), [Q]),
    ("db_password_abc123", _assign("DB_PASSWORD", "abc" + "123", "'"), [Q]),
    ("password_port_like", _assign("PASSWORD", "54" + "32", sep=": "), [Q]),
    ("secret_ip_like", _assign("SECRET", "10.0." + "0.1"), [Q]),
    ("password_ip_like", _assign("db_password", "10.0." + "0.1", "'"), [Q]),
    ("passwd", _assign("passwd", "x"), [Q]),
    ("pwd", _assign("MYSQL_PWD", "x", sep="="), [Q]),
    ("passphrase_spaces", _assign("Passphrase", "correct horse battery"), [Q]),
    ("lowercase_api_key", _assign("api_key", "k"), [Q]),
    # No judgement on how a value looks: a placeholder is still a literal.
    ("doc_placeholder", _assign("PASSWORD", "<YOUR" + "_KEY>", sep="="), [Q]),
    ("leading_space", _assign("password", " x"), [Q]),
]
SILENT = [
    ("getenv_call", "password" + EQ + 'getenv("DB_PASS")'),
    ("os_environ", "password" + EQ + 'os.environ["DB_PASS"]'),
    ("shell_var", "PASSWORD=" + "$PASS"),  # vco-secret-scan: test-placeholder
    ("quoted_brace_expansion", _assign("PASSWORD", "${DB_PASS}", sep="=")),
    ("quoted_dollar_var", _assign("PASSWORD", "$PASS", sep="=")),
    ("format_template", _assign("password", "{db_password}", sep=": ")),
    ("empty_double", _assign("password", "")),
    ("empty_single", _assign("password", "", "'")),
    ("f_string", "password" + EQ + 'f"{x}"'),
    ("unquoted_in_code_is_an_expression", "PASSWORD=" + "hunter" + "2"),  # vco-secret-scan: test-placeholder
]


# ── 1. the SSOT decides (both halves) ────────────────────────────────────


@pytest.mark.parametrize("name, text, expected", ALERTS,
                         ids=[a[0] for a in ALERTS])
def test_short_quoted_literal_alerts(name, text, expected):
    got = [lb for lb in matching_labels(text, "content_scan", CODE)
           if lb in GEN]
    assert got == expected, name


@pytest.mark.parametrize("name, text", SILENT, ids=[s[0] for s in SILENT])
def test_reference_or_empty_value_is_silent(name, text):
    got = [lb for lb in matching_labels(text, "content_scan", CODE)
           if lb in GEN]
    assert got == [], name


# (name, text, file name, expected generic labels) -- review F2 by file kind.
FILE_KIND_CASES = [
    ("env_unquoted", "DB_PWD=" + "hunter" + "2", ".env", [U]),  # vco-secret-scan: test-placeholder
    ("env_dollar_reference", "DB_PWD=" + "$X", ".env", []),  # vco-secret-scan: test-placeholder
    ("py_call_is_an_expression", "password" + EQ + "get_password()", "app.py", []),
    ("yaml_unquoted", "password: " + "hunter" + "2", "config.yaml", [UY]),  # vco-secret-scan: test-placeholder
    # Review R2 V-2: YAML booleans are syntax (any case), single letters and
    # words that merely start like one are values; outside YAML a literal.
    ("yaml_boolean_no", "password: " + "no", "config.yaml", []),  # vco-secret-scan: test-placeholder
    ("yaml_boolean_false_comment", "password: " + "False # off", "c.yml", []),  # vco-secret-scan: test-placeholder
    ("yaml_boolean_on_upper", "api_key: " + "ON", "c.yaml", []),  # vco-secret-scan: test-placeholder
    ("yaml_single_letter_y", "password: " + "y", "config.yaml", [UY]),  # vco-secret-scan: test-placeholder
    ("yaml_word_offset", "password: " + "offset", "config.yaml", [UY]),  # vco-secret-scan: test-placeholder
    ("env_no_is_a_literal", "PASSWORD=" + "no", ".env", [U]),  # vco-secret-scan: test-placeholder
    ("dockerfile_env", "ENV DB_PASSWORD " + "hunter" + "2", "Dockerfile", [U]),
    # Backticks: a JS literal in code, shell command substitution (syntax)
    # in an env/config-style file.
    ("js_backtick_literal", "const password" + EQ + "`x`;", "app.js", [BT]),
    ("js_backtick_interpolation", "const password" + EQ + "`${x}`;", "app.js", []),
    ("md_inline_code", "Set `" + "API_KEY=" + "` and then `x`", "notes.md", []),  # vco-secret-scan: test-placeholder
    ("sh_dollar_pwd_volume", 'docker run -v "$' + 'PWD:/app" img', "run.sh", []),
    ("sh_backtick_substitution", "PASSWORD=" + "`cat f`", "run.sh", []),  # vco-secret-scan: test-placeholder
    ("env_backtick_substitution", "DB_PWD=" + "`cat f`", ".env", []),  # vco-secret-scan: test-placeholder
    ("long_unquoted_in_code", "DB_PASSWORD=" + "A" * 40, "settings.py", []),  # vco-secret-scan: test-placeholder
]


@pytest.mark.parametrize("name, text, fname, expected", FILE_KIND_CASES,
                         ids=[c[0] for c in FILE_KIND_CASES])
def test_unquoted_value_is_decided_by_file_kind(name, text, fname, expected):
    got = [lb for lb in matching_labels(text, "content_scan", fname)
           if lb in GEN]
    assert got == expected, name


def test_command_and_repo_scan_are_not_broadened():
    """Scope decision: the Bash command scanner and the repo scanner never
    carry the generic shape -- an ordinary ``mysql --password=...`` must not
    start forfeiting compression / failing a commit."""
    for ctx in ("command_scan", "repo_scan", "argv_name"):
        ids = {i for i, _l, _p in patterns_for_context(ctx)}
        assert not ids & {"generic_secret_quoted", "generic_secret_unquoted",
                          "generic_secret_backtick",
                          "generic_secret_unquoted_yaml"}, ctx
        assert matching_labels(_assign("password", "hunter" + "2"), ctx) == ()


# ── 2. every scanner implementation agrees ───────────────────────────────


def _write(path: Path, body) -> None:
    """Bytes are written verbatim (a CRLF case must reach the scanner with its
    ``\\r``; ``write_text`` would write LF on Linux)."""
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")


def _credscan_sh(path: Path) -> list:
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; scan_file_for_credentials "$2"', "_",
         str(LIB / "credscan.sh"), str(path)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _credscan_ps1(path: Path) -> list:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh not installed")
    lib = str(LIB / "credscan.ps1").replace("'", "''")
    target = str(path).replace("'", "''")
    proc = subprocess.run(
        [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-Command",
         f". '{lib}'; Scan-FileForCredentials -FilePath '{target}'"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _post_tool_security(sibling: str, tmp_path: Path, body: str,
                        fname: str = "app.py") -> list:
    """Run post-tool-security for a Write of ``body``; return the ledger's
    pattern labels (the hook scans via credscan_introduced.py)."""
    if sibling == "sh":
        cmd = ["bash", str(HOOKS / "post-tool-security.sh")]
    else:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("pwsh not installed")
        cmd = [pwsh, *PWSH_BASE_FLAGS, "-NonInteractive", "-File",
               str(HOOKS / "post-tool-security.ps1")]
    proj = tmp_path / f"proj_{sibling}"
    (proj / ".claude" / "logs").mkdir(parents=True)
    (proj / ".claude" / "scripts").mkdir(parents=True)
    # Swallow desktop toasts.
    (proj / ".claude" / "scripts" / "notify.py").write_text(
        "pass\n", encoding="utf-8")
    target = proj / fname
    _write(target, body)
    text = body.decode("utf-8") if isinstance(body, bytes) else body
    env = {k: v for k, v in os.environ.items()
           if k not in ("VCT_DISABLE_HOOKS", "VCT_HOOK_LEAK_PROBE")}
    env["CLAUDE_PROJECT_DIR"] = str(proj)
    env["STDERR_CAP_DISABLE"] = "1"
    payload = {"hook_event_name": "PostToolUse", "tool_name": "Write",
               "session_id": "sess-w6v",
               "tool_input": {"file_path": str(target), "content": text}}
    proc = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=120, cwd=str(proj))
    assert proc.returncode == 0, (proc.stdout[-400:], proc.stderr[-400:])
    ledger = proj / ".claude" / "logs" / "credential_alerts.jsonl"
    labels: list = []
    if ledger.exists():
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                labels.extend(f["label"] for f in json.loads(ln)["findings"])
    return labels


_CODE_CASES = (
    [(n, t + "\n", bool(e)) for n, t, e in ALERTS]
    + [(n, t + "\n", False) for n, t in SILENT]
    # The marker silences its own line ...
    + [("marked_line", _assign("password", "hunter" + "2")
        + "  # " + TEST_PLACEHOLDER_MARKER + "\n", False)]
    + [("marked_placeholder", _assign("PASSWORD", "<YOUR" + "_KEY>", sep="=")
        + "  # " + TEST_PLACEHOLDER_MARKER + "\n", False)]
    # ... and only its own line.
    + [("marker_on_other_line", "# " + TEST_PLACEHOLDER_MARKER + "\n"
        + _assign("password", "hunter" + "2") + "\n", True)]
    # Review F1/F3/F4 forms, through every scanner implementation.
    + [("json_member", '"' + "password" + '": "' + "x" + '",' + "\n", True),
       ("python_annotated", "password" + ": str" + EQ + '"x"' + "\n", True),
       ("go_walrus", "password" + " :" + "= " + '"x"' + "\n", True),
       ("single_quoted_dollar", "PASSWORD=" + "'$x'" + "\n", True),  # vco-secret-scan: test-placeholder
       ("raw_prefix", "password" + EQ + 'r"x"' + "\n", True),
       ("triple_double", "password" + EQ + '"""x"""' + "\n", True),
       ("equality", "if password " + "== " + '"x":' + "\n", False),
       ("suffix_compound_key", '"' + "password_hint" + '": "' + "x" + '"' + "\n", False),
       ("backtick_in_python_is_not_a_literal", "password" + EQ + "`x`" + "\n", False)]
    # Case-sensitive everywhere: .NET -match would flag this on Windows only.
    + [("lowercase_akia_run", "x = " + "akia" + "Q" * 16 + "\n", False)]
)
_MARK = "  # " + TEST_PLACEHOLDER_MARKER
# (name, body, alerts, file name). Code cases run in a .py file; the file-kind
# cases (review F2) each run in their own kind, with and without the marker.
_SCANNER_CASES: list[tuple[str, str | bytes, bool, str]] = []
_SCANNER_CASES.extend((n, b, a, "f.py") for n, b, a in _CODE_CASES)
_SCANNER_CASES.extend((n, t + "\n", bool(e), f) for n, t, f, e in FILE_KIND_CASES)
_SCANNER_CASES.extend((n + "_marked", t + _MARK + "\n", False, f)
                      for n, t, f, _e in FILE_KIND_CASES)
# re-review 2 #8: a CRLF line with an empty value is silent in every
# scanner -- grep keeps the `\r`, so credscan.sh strips CRs first.
_SCANNER_CASES.append(("crlf_empty_value", b"DB_PASSWORD=\r\n", False, ".env"))
_IDS = [c[0] for c in _SCANNER_CASES]


@pytest.mark.parametrize("name, body, alerts, fname", _SCANNER_CASES, ids=_IDS)
def test_reconciler_scanner_sh(tmp_path, name, body, alerts, fname):
    f = tmp_path / fname
    _write(f, body)
    assert bool(_credscan_sh(f)) is alerts, name


@pytest.mark.parametrize("name, body, alerts, fname", _SCANNER_CASES, ids=_IDS)
def test_reconciler_scanner_ps1(tmp_path, name, body, alerts, fname):
    f = tmp_path / fname
    _write(f, body)
    assert bool(_credscan_ps1(f)) is alerts, name


@pytest.mark.parametrize("sibling", ["sh", "ps1"])
@pytest.mark.parametrize("name, body, alerts, fname", _SCANNER_CASES, ids=_IDS)
def test_post_tool_security(tmp_path, sibling, name, body, alerts, fname):
    labels = _post_tool_security(sibling, tmp_path, body, fname)
    assert bool(labels) is alerts, (name, labels)


@pytest.mark.parametrize("name, body, alerts, fname", _SCANNER_CASES, ids=_IDS)
def test_every_scanner_gives_the_same_labels(tmp_path, name, body, alerts,
                                             fname):
    """.sh / .ps1 / .py verdict parity on the label set, not only on
    alert-vs-silent."""
    if not shutil.which("pwsh"):
        pytest.skip("pwsh not installed")
    f = tmp_path / fname
    _write(f, body)
    sh = sorted(set(_credscan_sh(f)))
    ps = sorted(set(_credscan_ps1(f)))
    py = sorted(set(_post_tool_security("sh", tmp_path, body, fname)))
    assert sh == ps == py, (name, sh, ps, py)
