# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-language parity + decision tests for the credential-SHAPE vocabulary.

The vocabulary had forked into five hand-maintained copies. They drifted until
the content scanners could not see an OpenRouter key (``sk-or-v1-``) OR any
modern OpenAI project key (``sk-proj-`` / ``sk-svcacct-`` / ``sk-admin-``),
while still carrying the label "Anthropic/OpenAI API key" — a scanner claiming
coverage it did not have. These tests exist so that cannot recur silently.

Four implementations must agree, all pinned to
``tests/fixtures/credential_shape_parity.json``:

* Python  — ``vco_lib/credential_shapes.py`` (the SSOT)
* bash    — ``templates/hooks/_lib/credshapes.sh`` (hooks deployment root)
* pwsh    — ``templates/hooks/_lib/credshapes.ps1`` (Windows parity)
* bash    — ``tools/vct-secrets/lib/credential_shapes.sh`` (``vct`` root,
  ``argv_name`` projection only)

Plus structural tests proving no consuming site kept a private copy, and a
SUPERSET test protecting a site this lane deliberately did NOT consolidate
(see ``test_command_scan_is_strict_superset_of_lean_ctx_token_literal``).

No test here prints a candidate value. Fixture values are synthetic, built from
character repeats so a miscount cannot silently weaken a case.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from vco_lib.credential_shapes import (  # noqa: E402
    CONTEXTS,
    BACKTICK_CODE_NAME_SUFFIXES,
    ENV_CONFIG_EXACT_NAMES,
    ENV_CONFIG_NAME_INFIXES,
    ENV_CONFIG_NAME_PREFIXES,
    ENV_CONFIG_NAME_SUFFIXES,
    FILE_KINDS,
    SHAPES,
    YAML_NAME_SUFFIXES,
    combined_pattern,
    file_kind,
    matching_labels,
    patterns_for_context,
)

FIXTURE = REPO / "tests" / "fixtures" / "credential_shape_parity.json"
BASH_MIRROR = REPO / "templates" / "hooks" / "_lib" / "credshapes.sh"
PS1_MIRROR = REPO / "templates" / "hooks" / "_lib" / "credshapes.ps1"
VCT_MIRROR = REPO / "tools" / "vct-secrets" / "lib" / "credential_shapes.sh"
VCT_CLI = REPO / "tools" / "vct-secrets" / "vct"

ID2LABEL = {s.id: s.label for s in SHAPES}


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _file_kinds() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["file_kinds"]


def _ids_for(text: str, context: str, path: "str | None" = None) -> list[str]:
    """Shape ids matching ``text`` in ``context`` (for the file ``path`` when
    given), per the Python SSOT."""
    labels = set(matching_labels(text, context, path))
    return [s.id for s in SHAPES if context in s.patterns and s.label in labels]


# ---------------------------------------------------------------------------
# Fixture sanity — the fixture must be a real test, not a rubber stamp.
# ---------------------------------------------------------------------------


def test_fixture_is_substantial_and_covers_every_context():
    cases = _cases()
    assert len(cases) >= 30, f"fixture has only {len(cases)} cases"
    seen = {c["context"] for c in cases}
    assert seen == set(CONTEXTS), f"contexts not all exercised: {sorted(seen)}"
    # Both decisions must be exercised: act AND leave-alone.
    assert any(c["expect_ids"] for c in cases), "no positive-detection case"
    assert any(not c["expect_ids"] for c in cases), "no leave-alone case"


def test_fixture_contains_no_real_looking_token_literals():
    """Every fixture value must be synthetic (repeats) or AWS's public doc key.

    Guards against someone 'improving' the fixture with a captured real token.
    """
    allowed_doc_key = "AKIAIOSFODNN7EXAMPLE"  # vco-secret-scan: test-placeholder
    for case in _cases():
        text = case["text"]
        body = text.replace(allowed_doc_key, "")
        # A synthetic tail is a run of one repeated character. Assert no long
        # high-entropy mixed-case+digit run survives.
        for run in re.findall(r"[A-Za-z0-9]{20,}", body):
            distinct = set(run)
            assert len(distinct) <= 4, (
                f"case {case['name']!r} carries a high-entropy {len(run)}-char run "
                f"({len(distinct)} distinct chars) — fixtures must be synthetic repeats"
            )


# ---------------------------------------------------------------------------
# Leg 1 — Python SSOT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_python_ssot_matches_fixture(case):
    got = _ids_for(case["text"], case["context"], case.get("path"))
    assert got == case["expect_ids"], (
        f"{case['name']} [{case['context']}]: expected {case['expect_ids']}, got {got}"
    )


# ---------------------------------------------------------------------------
# Leg 2 — bash mirror (hooks deployment root)
# ---------------------------------------------------------------------------

_BASH_EVAL = r"""
set -u
. "$MIRROR"
if [ -n "${CS_HAS_PATH:-}" ]; then
    credshapes_for_context "$CS_CTX" "$CS_PATH" || { echo "__BAD_CONTEXT__"; exit 0; }
else
    credshapes_for_context "$CS_CTX" || { echo "__BAD_CONTEXT__"; exit 0; }
fi
for i in "${!CSHAPES_PATTERNS[@]}"; do
    if printf '%s' "$CS_TEXT" | LC_ALL=C grep -qE -- "${CSHAPES_PATTERNS[$i]}"; then
        printf '%s\n' "${CSHAPES_IDS[$i]}"
    fi
done
"""


def _bash_ids(mirror: Path, text: str, context: str,
              path: "str | None" = None) -> list[str]:
    env = dict(os.environ, MIRROR=str(mirror), CS_TEXT=text, CS_CTX=context)
    if path is not None:
        env.update(CS_HAS_PATH="1", CS_PATH=path)
    out = subprocess.run(
        ["bash", "-c", _BASH_EVAL], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"bash mirror failed: {out.stderr[:400]}"
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_bash_mirror_matches_fixture(case):
    got = _bash_ids(BASH_MIRROR, case["text"], case["context"], case.get("path"))
    assert got == case["expect_ids"], (
        f"bash mirror drifted on {case['name']} [{case['context']}]: "
        f"expected {case['expect_ids']}, got {got}"
    )


# ---------------------------------------------------------------------------
# Leg 3 — PowerShell mirror
# ---------------------------------------------------------------------------

_PWSH = shutil.which("pwsh") or shutil.which("powershell")

_PS_EVAL = r"""
$ErrorActionPreference = 'Stop'
. $env:MIRROR
$doc = Get-Content -LiteralPath $env:FIXTURE -Raw | ConvertFrom-Json
foreach ($case in $doc.cases) {
    $ids = @()
    if ($case.PSObject.Properties.Name -contains 'path') {
        $shapes = Get-CredShapes -Context $case.context -Path $case.path
    } else {
        $shapes = Get-CredShapes -Context $case.context
    }
    foreach ($s in $shapes) {
        if ($case.text -cmatch $s.Re) { $ids += $s.Id }
    }
    Write-Output ("{0}`t{1}" -f $case.name, ($ids -join ','))
}
"""


@pytest.mark.skipif(_PWSH is None, reason="PowerShell not available on this host")
def test_powershell_mirror_matches_fixture():
    env = dict(os.environ, MIRROR=str(PS1_MIRROR), FIXTURE=str(FIXTURE))
    out = subprocess.run(
        [cast(str, _PWSH), *PWSH_BASE_FLAGS, "-Command", _PS_EVAL],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, f"pwsh mirror failed: {out.stderr[:800]}"
    got = {}
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        name, ids = line.split("\t", 1)
        got[name.strip()] = [i for i in ids.strip().split(",") if i]
    mismatches = []
    for case in _cases():
        assert case["name"] in got, f"pwsh produced no row for {case['name']}"
        if got[case["name"]] != case["expect_ids"]:
            mismatches.append((case["name"], case["context"], case["expect_ids"], got[case["name"]]))
    assert not mismatches, f"PowerShell mirror drifted: {mismatches}"


# ---------------------------------------------------------------------------
# File kinds (owner ruling 2026-10-13, W6-V review F2) — every leg agrees on
# file_kind(path), and the mirrors carry the SSOT's table verbatim.
# ---------------------------------------------------------------------------


def test_fixture_file_kinds_cover_both_kinds():
    kinds = {r["kind"] for r in _file_kinds()}
    assert kinds == set(FILE_KINDS)
    assert len(_file_kinds()) >= 20


@pytest.mark.parametrize("row", _file_kinds(), ids=lambda r: r["path"] or "<empty>")
def test_python_file_kind_matches_fixture(row):
    assert file_kind(row["path"]) == row["kind"]


@pytest.mark.parametrize("row", _file_kinds(), ids=lambda r: r["path"] or "<empty>")
def test_bash_file_kind_matches_fixture(row):
    out = subprocess.run(
        ["bash", "-c", 'set -u; . "$1"; credshapes_file_kind "$2"', "_",
         str(BASH_MIRROR), row["path"]],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[:400]
    assert out.stdout == row["kind"], row


@pytest.mark.skipif(_PWSH is None, reason="PowerShell not available on this host")
def test_powershell_file_kind_matches_fixture():
    script = r"""
$ErrorActionPreference = 'Stop'
. $env:MIRROR
$doc = Get-Content -LiteralPath $env:FIXTURE -Raw | ConvertFrom-Json
foreach ($r in $doc.file_kinds) {
    Write-Output ("{0}`t{1}" -f $r.path, (Get-CredShapeFileKind -Path $r.path))
}
"""
    env = dict(os.environ, MIRROR=str(PS1_MIRROR), FIXTURE=str(FIXTURE))
    out = subprocess.run([cast(str, _PWSH), *PWSH_BASE_FLAGS, "-Command", script],
                         env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[:800]
    got = [ln.split("\t", 1) for ln in out.stdout.splitlines() if "\t" in ln]
    want = [[r["path"], r["kind"]] for r in _file_kinds()]
    assert got == want


def _sh_array(text: str, name: str) -> tuple:
    m = re.search(name + r"=\(([^)]*)\)", text)
    assert m, f"{name} not found in the bash mirror"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def _ps_array(text: str, name: str) -> tuple:
    m = re.search(r"\$" + name + r" = @\(([^)]*)\)", text)
    assert m, f"${name} not found in the PowerShell mirror"
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def test_mirrors_carry_the_ssot_file_kind_table():
    sh = BASH_MIRROR.read_text(encoding="utf-8")
    ps = PS1_MIRROR.read_text(encoding="utf-8-sig")
    for ssot, sh_name, ps_name in (
        (ENV_CONFIG_EXACT_NAMES, "CSHAPES_ENV_CONFIG_EXACT_NAMES", "exact"),
        (ENV_CONFIG_NAME_PREFIXES, "CSHAPES_ENV_CONFIG_NAME_PREFIXES", "prefixes"),
        (ENV_CONFIG_NAME_INFIXES, "CSHAPES_ENV_CONFIG_NAME_INFIXES", "infixes"),
        (ENV_CONFIG_NAME_SUFFIXES, "CSHAPES_ENV_CONFIG_NAME_SUFFIXES", "suffixes"),
        (YAML_NAME_SUFFIXES, "CSHAPES_YAML_NAME_SUFFIXES", "yaml"),
        (BACKTICK_CODE_NAME_SUFFIXES, "CSHAPES_BACKTICK_CODE_NAME_SUFFIXES", "backtick"),
    ):
        assert _sh_array(sh, sh_name) == ssot, sh_name
        assert _ps_array(ps, ps_name) == ssot, ps_name
    # Each restricted shape serves exactly one kind, and both mirrors carry
    # the same id -> kind table.
    restricted = tuple((s.id, s.file_kinds[0]) for s in SHAPES if s.file_kinds)
    assert all(s.file_kinds is None or len(s.file_kinds) == 1 for s in SHAPES)
    assert restricted == (("generic_secret_backtick", "backtick_code"),
                          ("generic_secret_unquoted", "env_config"),
                          ("generic_secret_unquoted_yaml", "yaml"))
    assert _sh_array(sh, "CSHAPES_KIND_RESTRICTED") == tuple(
        f"{i}:{k}" for i, k in restricted)
    ps_pairs = tuple(re.findall(r"\$r\['([^']+)'\] = '([^']+)'", ps))
    assert ps_pairs == restricted


# ---------------------------------------------------------------------------
# Leg 4 — the vct mirror (argv_name projection only)
# ---------------------------------------------------------------------------

_VCT_EVAL = r"""
set -u
. "$MIRROR"
if _credshapes_is_argv_name_credential "$CS_TEXT"; then echo MATCH; else echo NOMATCH; fi
"""


@pytest.mark.parametrize(
    "case", [c for c in _cases() if c["context"] == "argv_name"], ids=lambda c: c["name"]
)
def test_vct_mirror_matches_fixture(case):
    env = dict(os.environ, MIRROR=str(VCT_MIRROR), CS_TEXT=case["text"])
    out = subprocess.run(
        ["bash", "-c", _VCT_EVAL], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"vct mirror failed: {out.stderr[:400]}"
    matched = out.stdout.strip() == "MATCH"
    assert matched == bool(case["expect_ids"]), (
        f"vct mirror drifted on {case['name']}: expected "
        f"{'a match' if case['expect_ids'] else 'no match'}"
    )


def test_vct_mirror_alternation_equals_ssot_argv_name():
    """The vct mirror's alternation is byte-identical to the SSOT's."""
    text = VCT_MIRROR.read_text(encoding="utf-8")
    m = re.search(r"_CSHAPES_ARGV_NAME_RE='([^']*)'", text)
    assert m, "could not find _CSHAPES_ARGV_NAME_RE in the vct mirror"
    assert m.group(1) == combined_pattern("argv_name")


# ---------------------------------------------------------------------------
# Structural — the mirrors' own names do not read as secrets to bash_security
# ---------------------------------------------------------------------------
# W6-V review R2 finding V-1: `bash_security`'s `echo_secret_var` /
# `secret_var_expansion` rules match any `$NAME` containing KEY / TOKEN /
# SECRET / PASS / CRED (case-insensitive), so the mirror arrays' former
# `CRED`-prefixed stem made echoing them a BLOCKED command. The fix is the
# names (the `CSHAPES_*` stem), never a weaker needle: the control rows
# below prove real secret-shaped names are still refused. The stem also
# carries no `VCO_` / `VCT_` prefix, because these are internal shell arrays,
# not env keys (tests/test_v0292_env_keys_documented.py treats every
# `VCO_*` / `VCT_*` name a shipped script reads as an env key to document).

_SHELL_NAME_FILES = (
    BASH_MIRROR,
    BASH_MIRROR.parent / "credscan.sh",
    VCT_MIRROR,
    # The vct CLI's own alias of the argv_name alternation (R3 V-c). vct is a
    # secrets CLI whose OTHER variables (`key`, `token`, ...) are secret-shaped
    # on purpose and never echoed, so only its shape-vocabulary names are
    # checked there (_SHELL_NAME_FILTERS).
    VCT_CLI,
)
#: Per-file name filter: a file listed here contributes only the assigned
#: names the predicate keeps; every other file contributes all its names.
_SHELL_NAME_FILTERS = {
    VCT_CLI: lambda name: "SHAPES" in name.upper(),
}
_ASSIGNED_NAME_RE = re.compile(
    r"(?m)^[ \t]*(?:local[ \t]+|readonly[ \t]+|export[ \t]+|declare[ \t]+-\w+[ \t]+)?"
    r"([A-Za-z_]\w*)\+?="
)


def _bash_security():
    sys.path.insert(0, str(REPO / "templates" / "scripts"))
    try:
        import importlib
        import bash_security  # pyright: ignore[reportMissingImports]  # resolved by the sys.path.insert above
        importlib.reload(bash_security)
    finally:
        sys.path.pop(0)
    return bash_security


def _mirror_assigned_names() -> set:
    names: set = set()
    for path in _SHELL_NAME_FILES:
        found = _ASSIGNED_NAME_RE.findall(path.read_text(encoding="utf-8"))
        keep = _SHELL_NAME_FILTERS.get(path)
        names.update(n for n in found if keep is None or keep(n))
    return names


def test_mirror_variable_names_are_not_secret_shaped():
    names = _mirror_assigned_names()
    # Non-vacuous: the output contract and the vct alternation are found.
    assert {"CSHAPES_IDS", "CSHAPES_LABELS", "CSHAPES_PATTERNS",
            "CSHAPES_TEST_PLACEHOLDER_MARKER", "_CSHAPES_ARGV_NAME_RE",
            "_VCT_CSHAPES_ARGV_NAME_RE"} <= names
    bs = _bash_security()
    blocked = {}
    for name in sorted(names):
        for cmd in (f'echo "${name}"', f"echo ${{{name}}}",
                    f'printf "%s," ${{{name}[@]}}', f'printf "%s\\n" "${name}"'):
            ok, reason = bs.check_command(cmd)
            if not ok:
                blocked[cmd] = reason.splitlines()[0] if reason else ""
    assert not blocked, f"mirror names read as secrets: {blocked}"


@pytest.mark.parametrize("cmd", [
    'echo "$MY_CRED_TOKEN"',
    "echo $MY_CRED_TOKEN",
    'printf "%s" "${DB_PASSWORD}"',
    "echo $CREDENTIALS",
])
def test_real_secret_shaped_names_still_blocked(cmd):
    ok, reason = _bash_security().check_command(cmd)
    assert not ok, f"needle weakened: {cmd!r} passed"
    assert "secret" in reason.lower(), reason


# ---------------------------------------------------------------------------
# Structural — no consuming site kept a private copy
# ---------------------------------------------------------------------------

#: Literals that only ever appeared inside a PRIVATE fork of the vocabulary.
#: Their reappearance in a consuming site means someone re-inlined a copy.
_FORK_FINGERPRINTS = (
    r"sk-\(ant-api03",                      # the old vendor-blind sk- pattern
    r"AKIA\[A-Z0-9\]\{16\}",                # inlined AWS shape
    r"gh\[pousr\]_\[a-zA-Z0-9\]\{36\}",     # inlined GitHub shape
    r"github_pat_\[A-Za-z0-9\]\{22\}_",     # inlined fine-grained shape
    r"BEGIN \(RSA \|EC \|OPENSSH \|\)",     # the old narrow PEM alternation
)

_CONSUMING_SITES = (
    "scripts/check-no-secrets.sh",
    "templates/hooks/_lib/credscan.sh",
    "templates/hooks/_lib/credscan.ps1",
    "templates/hooks/post-tool-security.sh",
    "templates/hooks/post-tool-security.ps1",
    "tools/vct-secrets/vct",
)


@pytest.mark.parametrize("rel", _CONSUMING_SITES)
def test_consuming_site_has_no_private_vocabulary_copy(rel):
    """Every consumer reads the shared vocabulary; none re-inlines one.

    A shared home with the old copies still beside it is worse than either
    state alone — the copies look authoritative and drift unobserved.
    """
    text = (REPO / rel).read_text(encoding="utf-8")
    offenders = [f for f in _FORK_FINGERPRINTS if re.search(f, text)]
    assert not offenders, (
        f"{rel} re-inlines credential shape(s) {offenders}; it must read them "
        f"from the shared vocabulary instead"
    )


@pytest.mark.parametrize("rel", _CONSUMING_SITES)
def test_consuming_site_actually_loads_the_vocabulary(rel):
    """Absence of a private copy is not enough — prove the site LOADS the SSOT.

    Without this, deleting a scanner's patterns entirely would pass the
    no-private-copy test while silently scanning for nothing.
    """
    text = (REPO / rel).read_text(encoding="utf-8")
    assert re.search(r"credshapes(\.sh|\.ps1)|credential_shapes\.sh|Get-CredShapes|_CSHAPES_", text), (
        f"{rel} does not reference the shared credential-shape vocabulary"
    )


def test_every_context_is_reachable_and_nonempty():
    for ctx in CONTEXTS:
        rows = patterns_for_context(ctx)
        assert rows, f"context {ctx} resolves to zero shapes"


def test_context_selector_is_explicit_and_rejects_unknown():
    """No default context: a caller must choose its precision bias knowingly."""
    with pytest.raises(ValueError):
        patterns_for_context("not_a_context")
    with pytest.raises(TypeError):
        patterns_for_context()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The precision-context contract, stated as tests
# ---------------------------------------------------------------------------


def test_repo_scan_omits_base64_collision_prone_shapes():
    """repo_scan must NOT carry the shapes that collide with vendored base64.

    Pinned because the collision is REAL and still in the tree: the vendored
    Excalidraw bundle's base64 WASM/font chunk contains an ``AKIA`` + 16
    upper-alnum run by chance, and ``eyJ`` is simply base64 for '{"'. Adding
    these to repo_scan makes scripts/check-no-secrets.sh fail on every commit.
    """
    repo_ids = {i for i, _l, _p in patterns_for_context("repo_scan")}
    for forbidden in ("aws_access_key_id", "jwt", "atlassian_token"):
        assert forbidden not in repo_ids, (
            f"{forbidden} was added to repo_scan; it collides with base64 payloads "
            f"in vendored bundles — see the SSOT docstring"
        )
        # ...and it must still be present where it IS safe and useful.
        content_ids = {i for i, _l, _p in patterns_for_context("content_scan")}
        assert forbidden in content_ids, f"{forbidden} lost from content_scan"


def test_file_scan_narrowing_still_holds_for_the_live_corpus():
    """The Slovak locale asset name must not be flagged by any file-scan context.

    This is the constraint the narrowed sk- tail exists to preserve. The corpus
    is live: vco_lib/excalidraw_mcp_fork/dist/canvas/frontend/assets/index-*.js
    is tracked and sits inside check-no-secrets.sh's dist scan set.
    """
    asset = "sk-SK-C5VTKIMK-BuPPqNUL"
    for ctx in ("repo_scan", "content_scan", "command_scan"):
        assert matching_labels(asset, ctx) == (), (
            f"{ctx} now flags the vendored Excalidraw locale asset name"
        )


def test_argv_name_is_wide_where_file_scan_is_narrow():
    """argv_name keeps the WIDE sk- arm; a name cannot be an asset-name haystack."""
    assert matching_labels("sk-" + "-".join(["ab"] * 8), "argv_name")
    assert matching_labels("sk-" + "-".join(["ab"] * 8), "repo_scan") == ()


def test_plausible_human_key_names_are_not_refused():
    """argv_name must not refuse legitimate hand-typed key names."""
    for name in ("github_pat_personal", "MYPROJECT_JIRA_TOKEN", "openai_api_key", "ghp_notes"):
        assert matching_labels(name, "argv_name") == (), f"{name} wrongly refused"


def test_labels_do_not_overclaim_vendor_coverage():
    """A label must not name a vendor whose current key shape it cannot match.

    The defect this consolidation fixed was a label ("Anthropic/OpenAI API key")
    that claimed coverage its pattern lacked. A wrong label is worse than a
    missing check: it suppresses the question.
    """
    sk = next(s for s in SHAPES if s.id == "sk_vendor_key")
    for vendor_key in (
        "sk-or-v1-" + "a" * 64,          # OpenRouter
        "sk-proj-" + "A" * 48,           # OpenAI project
        "sk-svcacct-" + "A" * 48,        # OpenAI service account
        "sk-admin-" + "A" * 48,          # OpenAI admin
        "sk-ant-api03-" + "A" * 95,      # Anthropic
    ):
        for ctx in ("repo_scan", "content_scan"):
            assert re.search(sk.patterns[ctx], vendor_key), (
                f"label {sk.label!r} claims coverage this pattern does not have"
            )


# ---------------------------------------------------------------------------
# SUPERSET guard for a site this lane deliberately did NOT consolidate.
# ---------------------------------------------------------------------------

LEAN_CTX_SH = REPO / "templates" / "hooks" / "lean-ctx-rewrite.sh"


def _lean_ctx_token_literal() -> str:
    """Extract the SEC-RAW TOKEN_LITERAL alternation. Read-only."""
    text = LEAN_CTX_SH.read_text(encoding="utf-8")
    block = text[text.index("SEC-RAW-PATTERNS-BEGIN"):text.index("SEC-RAW-PATTERNS-END")]
    m = re.search(r'r"(\\b\(\?:ATATT.*?)",', block)
    assert m, "TOKEN_LITERAL alternation not found in lean-ctx-rewrite.sh"
    return m.group(1)


def test_command_scan_is_strict_superset_of_lean_ctx_token_literal():
    """command_scan must catch everything lean-ctx's private list catches.

    ``templates/hooks/lean-ctx-rewrite.{sh,ps1}`` was deliberately left OUT of
    this consolidation: it is a PreToolUse hook intercepting every Bash call,
    with a history of indirect breakage, and — uniquely among the sites — its
    private copy is HARMLESS when stale. Its failure modes are asymmetric: too
    broad merely forfeits output compression, too narrow routes a
    credential-bearing command through a wrapper that is not credential-aware
    (a real, already-observed auth failure). So a stale copy there drifts toward
    catching MORE, which is the safe direction.

    This test keeps the consolidation OPTION open and costed: it proves the
    shared command_scan context would be a STRICT SUPERSET of that private list,
    so adopting it later cannot narrow the guard. It also stops anyone from
    quietly narrowing command_scan in the SSOT.

    It does not modify lean-ctx-rewrite.sh; it only reads it.
    """
    lean = re.compile(_lean_ctx_token_literal())
    probes = [c["text"] for c in _cases()]
    probes += [
        "ATATT" + "A" * 24, "ghp_" + "A" * 12, "github_pat_" + "A" * 12,
        "ghs_" + "A" * 12, "glpat-" + "A" * 12, "xoxb-" + "A" * 12,
        "AKIAIOSFODNN7EXAMPLE", "eyJ" + "A" * 30,  # vco-secret-scan: test-placeholder
        "curl -H 'X: ghp_" + "A" * 36 + "'",
    ]
    narrowed = [
        p for p in probes
        if lean.search(p) and not matching_labels(p, "command_scan")
    ]
    assert not narrowed, (
        f"command_scan would NARROW lean-ctx's SEC-RAW guard on {len(narrowed)} "
        f"probe(s) — consolidating site 2 onto it is unsafe until fixed"
    )


def test_lean_ctx_rewrite_is_untouched_by_this_lane():
    """Pin the deliberate non-consolidation so a future edit is a conscious act.

    If someone consolidates site 2 later, this test SHOULD be updated together
    with that change — it is a decision record, not an obstacle.
    """
    text = LEAN_CTX_SH.read_text(encoding="utf-8")
    assert "SEC-RAW-PATTERNS-BEGIN" in text
    assert "credshapes" not in text, (
        "lean-ctx-rewrite.sh now references the shared vocabulary. That is a "
        "legitimate change, but it must be made deliberately (strict-superset "
        "proof + step-aside red-proof) and this test updated to match."
    )


# ---------------------------------------------------------------------------
# Label parity — the user-visible strings must match across languages too.
# ---------------------------------------------------------------------------

_BASH_LABELS = r"""
set -u
. "$MIRROR"
credshapes_for_context "$CS_CTX" || exit 1
for i in "${!CSHAPES_IDS[@]}"; do
    printf '%s\t%s\n' "${CSHAPES_IDS[$i]}" "${CSHAPES_LABELS[$i]}"
done
"""

_PS_LABELS = r"""
$ErrorActionPreference = 'Stop'
. $env:MIRROR
foreach ($s in (Get-CredShapes -Context $env:CS_CTX)) {
    Write-Output ("{0}`t{1}" -f $s.Id, $s.Label)
}
"""


@pytest.mark.parametrize("context", CONTEXTS)
def test_bash_mirror_ids_and_labels_match_ssot(context):
    """Ids AND labels agree with the SSOT, in declaration order.

    Label drift matters on its own: a finding reported as one string on Linux
    and another on Windows breaks log aggregation and makes the same leak look
    like two different findings.
    """
    env = dict(os.environ, MIRROR=str(BASH_MIRROR), CS_CTX=context)
    out = subprocess.run(
        ["bash", "-c", _BASH_LABELS], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr[:400]
    got = [tuple(ln.split("\t", 1)) for ln in out.stdout.splitlines() if "\t" in ln]
    want = [(i, lbl) for i, lbl, _p in patterns_for_context(context)]
    assert got == want, f"bash mirror id/label drift in {context}"


@pytest.mark.skipif(_PWSH is None, reason="PowerShell not available on this host")
@pytest.mark.parametrize("context", CONTEXTS)
def test_powershell_mirror_ids_and_labels_match_ssot(context):
    env = dict(os.environ, MIRROR=str(PS1_MIRROR), CS_CTX=context)
    out = subprocess.run(
        [cast(str, _PWSH), *PWSH_BASE_FLAGS, "-Command", _PS_LABELS],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[:800]
    got = [tuple(ln.split("\t", 1)) for ln in out.stdout.splitlines() if "\t" in ln]
    want = [(i, lbl) for i, lbl, _p in patterns_for_context(context)]
    assert got == want, f"PowerShell mirror id/label drift in {context}"


# ---------------------------------------------------------------------------
# Payload pinning + a LIVE-corpus oracle.
#
# The mirrors cannot be byte-identical to each other (bash arrays vs
# PSCustomObject vs Python), so the closest available equivalent to
# "byte-pin the payload" is asserting the PATTERN STRINGS themselves are
# identical to the SSOT's, not merely behaviourally equivalent on the fixture.
# A regex that behaves the same on 40 fixture rows can still diverge on the
# 41st; string identity cannot.
#
# String identity still shares one weakness with any fingerprint check: it
# passes when every side is equally WRONG. The live-corpus test below is the
# independent oracle for that — it runs the real repo_scan patterns over the
# actual vendored bundle in this tree, so it cannot agree with a wrong SSOT.
# ---------------------------------------------------------------------------

_BASH_PATTERNS = r"""
set -u
. "$MIRROR"
credshapes_for_context "$CS_CTX" || exit 1
for i in "${!CSHAPES_PATTERNS[@]}"; do
    printf '%s\t%s\n' "${CSHAPES_IDS[$i]}" "${CSHAPES_PATTERNS[$i]}"
done
"""

_PS_PATTERNS = r"""
$ErrorActionPreference = 'Stop'
. $env:MIRROR
foreach ($s in (Get-CredShapes -Context $env:CS_CTX)) {
    Write-Output ("{0}`t{1}" -f $s.Id, $s.Re)
}
"""


@pytest.mark.parametrize("context", CONTEXTS)
def test_bash_mirror_pattern_strings_are_identical_to_ssot(context):
    env = dict(os.environ, MIRROR=str(BASH_MIRROR), CS_CTX=context)
    out = subprocess.run(
        ["bash", "-c", _BASH_PATTERNS], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr[:400]
    got = [tuple(ln.split("\t", 1)) for ln in out.stdout.splitlines() if "\t" in ln]
    want = [(i, p) for i, _l, p in patterns_for_context(context)]
    assert got == want, f"bash mirror pattern-string drift in {context}"


@pytest.mark.skipif(_PWSH is None, reason="PowerShell not available on this host")
@pytest.mark.parametrize("context", CONTEXTS)
def test_powershell_mirror_pattern_strings_are_identical_to_ssot(context):
    env = dict(os.environ, MIRROR=str(PS1_MIRROR), CS_CTX=context)
    out = subprocess.run(
        [cast(str, _PWSH), *PWSH_BASE_FLAGS, "-Command", _PS_PATTERNS],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[:800]
    got = [tuple(ln.split("\t", 1)) for ln in out.stdout.splitlines() if "\t" in ln]
    want = [(i, p) for i, _l, p in patterns_for_context(context)]
    assert got == want, f"PowerShell mirror pattern-string drift in {context}"


#: The vendored third-party bundles that scripts/check-no-secrets.sh's dist
#: passes actually walk. These are the REAL haystack the repo_scan precision
#: bias exists for.
_VENDORED_DIST = REPO / "vco_lib" / "excalidraw_mcp_fork" / "dist" / "canvas" / "frontend" / "assets"


@pytest.mark.skipif(not _VENDORED_DIST.is_dir(), reason="vendored Excalidraw bundle not present")
def test_repo_scan_finds_nothing_in_the_live_vendored_bundle():
    """LIVE-CORPUS ORACLE: repo_scan must be silent on the vendored bundle.

    Independent of the fixture and of the SSOT's own claims: it runs the real
    patterns over the real files that scripts/check-no-secrets.sh scans. Two
    genuine collisions live in this directory and motivated the precision bias:

      * a Slovak locale chunk name of the form ``sk-SK-<hash>-<hash>``;
      * an ``AKIA`` + 16-upper-alnum run occurring by chance inside a base64
        WASM/font payload.

    If this ever reds, either a pattern was widened carelessly or the vendored
    bundle was bumped and now contains a genuinely new colliding string — check
    which BEFORE relaxing anything. It never prints a matched value.
    """
    compiled = [(i, re.compile(p)) for i, _l, p in patterns_for_context("repo_scan")]
    offenders = []
    for path in sorted(_VENDORED_DIST.glob("*.js")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for shape_id, rx in compiled:
            n = len(rx.findall(text))
            if n:
                offenders.append(f"{path.name}: {shape_id} x{n}")
    assert not offenders, (
        "repo_scan matched inside the VENDORED bundle — scripts/check-no-secrets.sh "
        f"would now fail on every commit: {offenders}"
    )
